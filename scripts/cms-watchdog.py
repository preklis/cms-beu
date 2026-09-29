#!/usr/bin/env python3

# Contest Management System - BEU fork
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""CMS ranking/proxy watchdog: health monitoring and automatic restart.

Keeps cmsRankingWebServer and cmsProxyService alive:

- a service whose process died is restarted;
- a service that is alive but does not answer for several consecutive
  checks (hung) is killed and restarted;
- repeated crashes are restarted with an exponential backoff, so that a
  service that cannot start does not spin in a tight loop;
- nothing is started until ResourceService is reachable;
- services that were started by someone else and are healthy are left
  alone (no duplicates).

When ProxyService (re)starts it replays all the scores and tokens, so
the ranking is back-filled automatically.

Run "cms-watchdog.py --help" for the options, and see docs/WATCHDOG.md
and man.txt for the details.

"""

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


logger = logging.getLogger("cms-watchdog")

DEFAULT_CMS_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = "/var/local/log/cms"
CONFIG_PATHS = ["/usr/local/etc/cms.conf", "/etc/cms.conf"]


def load_cms_config(path=None):
    """Return the CMS configuration (as a dict), or {} if unavailable."""
    paths = list(CONFIG_PATHS)
    if os.environ.get("CMS_CONFIG"):
        paths.insert(0, os.environ["CMS_CONFIG"])
    if path is not None:
        paths = [path]
    for candidate in paths:
        try:
            with open(candidate, "rt", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            logger.warning("Cannot read CMS config %s: %s", candidate, error)
    return {}


def service_address(cms_config, section, name, default):
    """Return the (host, port) of shard 0 of a service in cms.conf."""
    try:
        host, port = cms_config[section][name][0]
        return host, int(port)
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def ranking_url(cms_config, default):
    """Return the URL of the first ranking, without credentials."""
    try:
        url = cms_config["rankings"][0]
    except (KeyError, IndexError, TypeError):
        return default
    parts = urlsplit(url)
    netloc = parts.hostname or "127.0.0.1"
    if parts.port is not None:
        netloc += ":%d" % parts.port
    return parts._replace(netloc=netloc).geturl()


def port_open(host, port, timeout=2.0):
    """Return whether something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_running(program):
    """Return whether a process running the given CMS program exists.

    Only command lines where the program appears as an executable (at
    the start, or after a "/", followed by a space or the end) match,
    so that e.g. "less cmsRankingWebServer.log" or an editor does not.

    """
    pattern = r"(^|[ /])%s( |$)" % program
    try:
        result = subprocess.run(["pgrep", "-f", pattern],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ManagedService:
    """A service started, checked and restarted by the watchdog."""

    def __init__(self, name, command, cwd, log_path, health_check,
                 process_pattern, args):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.log_path = log_path
        self.health_check = health_check
        self.process_pattern = process_pattern
        self.args = args

        self.process = None
        self.log_file = None
        self.started_at = None
        self.failed_checks = 0
        self.consecutive_restarts = 0
        self.next_start_allowed = 0.0

    # State.

    def own_process_alive(self):
        return self.process is not None and self.process.poll() is None

    def healthy(self):
        try:
            return self.health_check()
        except Exception:
            logger.debug("%s health check raised.", self.name, exc_info=True)
            return False

    def in_grace_period(self):
        return (self.started_at is not None
                and time.monotonic() - self.started_at < self.args.grace)

    # Actions.

    def start(self):
        now = time.monotonic()
        if now < self.next_start_allowed:
            logger.info("%s: waiting %.0fs before the next restart attempt.",
                        self.name, self.next_start_allowed - now)
            return False

        self._close_log()
        logger.info("Starting %s: %s", self.name, " ".join(self.command))
        try:
            self.log_file = open(self.log_path, "ab")
            self.process = subprocess.Popen(
                self.command, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=self.log_file, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as error:
            logger.error("Cannot start %s: %s", self.name, error)
            self._close_log()
            self._schedule_backoff()
            return False

        logger.info("%s started (PID %d, output in %s).",
                    self.name, self.process.pid, self.log_path)
        self.started_at = time.monotonic()
        self.failed_checks = 0
        self._schedule_backoff()
        return True

    def stop(self, reason):
        if not self.own_process_alive():
            self._close_log()
            return
        logger.warning("Stopping %s (PID %d): %s.",
                       self.name, self.process.pid, reason)
        self._signal_group(signal.SIGTERM)
        try:
            self.process.wait(timeout=self.args.stop_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("%s did not stop in %ds, killing it.",
                           self.name, self.args.stop_timeout)
            self._signal_group(signal.SIGKILL)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error("%s (PID %d) survived SIGKILL.",
                             self.name, self.process.pid)
        self._close_log()

    def _signal_group(self, sig):
        # The service runs in its own session: signal all its children.
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                self.process.send_signal(sig)
            except OSError:
                pass

    def _close_log(self):
        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None

    def _schedule_backoff(self):
        # Exponential backoff between (re)starts; reset once the service
        # has been running fine for a while (see check()).
        delay = min(self.args.backoff_max,
                    self.args.backoff_min * (2 ** self.consecutive_restarts))
        self.next_start_allowed = time.monotonic() + delay
        self.consecutive_restarts += 1

    # The main logic, called at every tick.

    def check(self):
        """Check the service and act. Return a short status string."""
        if self.healthy():
            self.failed_checks = 0
            if self.started_at is not None and \
                    time.monotonic() - self.started_at > self.args.stable_after:
                self.consecutive_restarts = 0
            return "ok"

        if self.own_process_alive():
            if self.in_grace_period():
                return "starting"
            self.failed_checks += 1
            if self.failed_checks < self.args.max_failed_checks:
                logger.warning("%s is running but not responding "
                               "(%d/%d).", self.name, self.failed_checks,
                               self.args.max_failed_checks)
                return "unresponsive"
            self.stop("not responding for %d checks" % self.failed_checks)
            self.process = None
        elif self.process is not None:
            logger.warning("%s exited with code %s.",
                           self.name, self.process.returncode)
            self.process = None
            self._close_log()
        elif self.process_pattern is not None \
                and process_running(self.process_pattern):
            # Started by someone else and not healthy: we cannot manage
            # it, and starting a second copy would only fail.
            logger.warning("%s is running outside the watchdog but not "
                           "responding; not touching it.", self.name)
            return "foreign"

        return "restarting" if self.start() else "down"


class Watchdog:

    def __init__(self, args):
        self.args = args
        self.running = True
        cms_config = load_cms_config(args.config)

        self.resource_address = service_address(
            cms_config, "core_services", "ResourceService",
            ("127.0.0.1", 28000))
        self.proxy_address = service_address(
            cms_config, "core_services", "ProxyService", ("127.0.0.1", 28600))
        self.ranking = args.ranking_url or ranking_url(
            cms_config, "http://127.0.0.1:8890/")

        def bin_path(name):
            local = os.path.join(args.cms_home, "scripts", name)
            return local if os.path.exists(local) else name

        proxy_cmd = [bin_path("cmsProxyService")]
        if args.contest == "ALL":
            proxy_cmd += ["-c", "ALL"]
        elif args.contest is not None:
            proxy_cmd += ["-c", args.contest]
        proxy_cmd += ["0"]

        self.services = []
        if not args.no_ranking:
            self.services.append(ManagedService(
                "RankingWebServer", [bin_path("cmsRankingWebServer")],
                args.cms_home, os.path.join(args.log_dir, "ranking.log"),
                self.ranking_healthy, "cmsRankingWebServer", args))
        if not args.no_proxy:
            self.services.append(ManagedService(
                "ProxyService", proxy_cmd, args.cms_home,
                os.path.join(args.log_dir, "proxy.log"),
                lambda: port_open(*self.proxy_address), "cmsProxyService",
                args))

    def ranking_healthy(self):
        url = self.ranking.rstrip("/") + "/contests/"
        try:
            with urllib.request.urlopen(url, timeout=self.args.http_timeout):
                return True
        except urllib.error.HTTPError as error:
            # Any HTTP answer (even 401/404) means the server is alive.
            return error.code < 500
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def resource_service_up(self):
        return port_open(*self.resource_address) \
            or process_running("cmsResourceService")

    def tick(self):
        if not self.resource_service_up():
            logger.warning("ResourceService is not reachable at %s:%d; "
                           "not starting anything until it is.",
                           *self.resource_address)
            return
        states = []
        for service in self.services:
            states.append("%s:%s" % (service.name, service.check()))
        logger.info("Health: %s", " | ".join(states))

    def status(self):
        """Print a one-shot health report; return a process exit code."""
        ok = True
        rs = self.resource_service_up()
        ok = ok and rs
        print("ResourceService  %-4s (%s:%d)" % (
            "up" if rs else "DOWN", *self.resource_address))
        for service in self.services:
            healthy = service.healthy()
            ok = ok and healthy
            where = self.ranking if service.name == "RankingWebServer" \
                else "%s:%d" % self.proxy_address
            print("%-16s %-4s (%s)" % (service.name,
                                       "up" if healthy else "DOWN", where))
        return 0 if ok else 1

    def stop(self, *_):
        logger.info("Shutdown requested.")
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("CMS watchdog started (CMS home %s, check every %ds, "
                    "ranking %s, proxy %s:%d, ResourceService %s:%d).",
                    self.args.cms_home, self.args.interval, self.ranking,
                    *self.proxy_address, *self.resource_address)
        while self.running:
            try:
                self.tick()
            except Exception:
                logger.error("Unexpected error in the watchdog loop.",
                             exc_info=True)
            # Sleep in small steps to react quickly to signals.
            deadline = time.monotonic() + self.args.interval
            while self.running and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

        if self.args.keep_services:
            logger.info("Leaving the services running (--keep-services).")
        else:
            for service in reversed(self.services):
                service.stop("watchdog shutting down")
        logger.info("CMS watchdog stopped.")


def parse_args(argv=None):
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="Keep cmsRankingWebServer and cmsProxyService running.")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "status"],
                        help="run the watchdog (default), or print the "
                        "health of the services and exit (0 if all up)")
    parser.add_argument("--cms-home", default=env("CMS_HOME", DEFAULT_CMS_HOME),
                        help="CMS checkout directory (default: %(default)s)")
    parser.add_argument("--config", default=None,
                        help="path of cms.conf (default: $CMS_CONFIG, "
                        "/usr/local/etc/cms.conf, /etc/cms.conf)")
    parser.add_argument("--ranking-url", default=env("CMS_WATCHDOG_RANKING_URL"),
                        help="ranking URL to probe (default: first entry of "
                        "\"rankings\" in cms.conf)")
    parser.add_argument("-c", "--contest", default=env("CMS_WATCHDOG_CONTEST", "ALL"),
                        help="contest id for ProxyService, or ALL for "
                        "multi-contest mode (default: %(default)s)")
    parser.add_argument("--log-dir", default=env("CMS_WATCHDOG_LOG_DIR", DEFAULT_LOG_DIR),
                        help="directory of the watchdog and service logs "
                        "(default: %(default)s)")
    parser.add_argument("--interval", type=int, default=int(env("CMS_WATCHDOG_INTERVAL", "15")),
                        help="seconds between health checks (default: %(default)s)")
    parser.add_argument("--max-failed-checks", type=int, default=4,
                        help="consecutive failed checks of a running service "
                        "before it is considered hung and restarted "
                        "(default: %(default)s)")
    parser.add_argument("--grace", type=int, default=30,
                        help="seconds a freshly started service may take to "
                        "become healthy (default: %(default)s)")
    parser.add_argument("--backoff-min", type=int, default=5,
                        help="minimum seconds between restarts (default: %(default)s)")
    parser.add_argument("--backoff-max", type=int, default=300,
                        help="maximum seconds between restarts (default: %(default)s)")
    parser.add_argument("--stable-after", type=int, default=600,
                        help="seconds of good health after which the backoff "
                        "resets (default: %(default)s)")
    parser.add_argument("--stop-timeout", type=int, default=15,
                        help="seconds to wait after SIGTERM before SIGKILL "
                        "(default: %(default)s)")
    parser.add_argument("--http-timeout", type=float, default=5.0,
                        help="timeout of the ranking HTTP probe (default: %(default)s)")
    parser.add_argument("--no-ranking", action="store_true",
                        help="do not manage cmsRankingWebServer")
    parser.add_argument("--no-proxy", action="store_true",
                        help="do not manage cmsProxyService")
    parser.add_argument("--keep-services", action="store_true",
                        help="leave the services running when the watchdog stops")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log debug messages")
    return parser.parse_args(argv)


def setup_logging(args):
    handlers = [logging.StreamHandler(sys.stdout)]
    if args.command == "run":
        try:
            os.makedirs(args.log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(
                os.path.join(args.log_dir, "watchdog.log")))
        except OSError as error:
            print("Cannot write logs in %s: %s" % (args.log_dir, error),
                  file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers)


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args)
    watchdog = Watchdog(args)
    if args.command == "status":
        return watchdog.status()
    watchdog.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
