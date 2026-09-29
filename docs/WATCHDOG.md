# CMS Ranking/Proxy Watchdog

Automatic monitoring and recovery for `cmsRankingWebServer` and
`cmsProxyService` (`scripts/cms-watchdog.py`). The full option reference
is in `man.txt` at the repository root.

## What it does

- **Restarts crashed services.** If a service's process exits, it is
  started again.
- **Restarts hung services.** If a service is alive but fails
  `--max-failed-checks` consecutive health checks (default 4), the
  watchdog sends SIGTERM to its whole process group, then SIGKILL after
  `--stop-timeout` seconds, and starts it again.
- **Backs off on crash loops.** Restarts are spaced exponentially, from
  `--backoff-min` (5s) up to `--backoff-max` (300s). The backoff resets
  once the service has been healthy for `--stable-after` seconds (600s).
- **Waits for ResourceService.** Nothing is started while
  ResourceService is unreachable. The watchdog never touches
  ResourceService itself.
- **Leaves other instances alone.** If an instance that the watchdog did
  not start is already running, the watchdog does not start a second
  copy. If that instance is unhealthy, the watchdog logs a warning and
  does not kill it.
- **Backfills the ranking.** When ProxyService starts, it replays every
  score and token, so the ranking catches up after downtime.

## Health checks

| Service         | Check                                                                     |
|-----------------|---------------------------------------------------------------------------|
| ResourceService | TCP connect to its RPC address from `cms.conf` (default `localhost:28000`), or a running `cmsResourceService` process |
| Ranking         | HTTP GET `<ranking>/contests/` using the first `rankings` URL in `cms.conf` (credentials stripped). Any response below 500 counts as healthy |
| Proxy           | TCP connect to the ProxyService RPC address from `cms.conf` (default `localhost:28600`) |

A newly started service has `--grace` seconds (30 by default) to become
healthy before failed checks count against it.

## Quick commands

```bash
scripts/cms-watchdog.py status     # one-shot health report (exit code 0 = all up)
cms-watchdog-util status           # systemd status + health report
cms-watchdog-util logs [N]         # last N lines of the watchdog log
cms-watchdog-util follow           # follow the watchdog log
cms-watchdog-util service-logs     # tail ranking.log and proxy.log
cms-watchdog-util restart          # restart the watchdog (systemd)
```

## Installation (systemd)

```bash
sudo cp scripts/cms-watchdog-util /usr/local/bin/
sudo cp config/cms-watchdog.service.sample /etc/systemd/system/cms-watchdog.service
sudoedit /etc/systemd/system/cms-watchdog.service   # set User=, CMS_HOME, paths
sudo systemctl daemon-reload
sudo systemctl enable --now cms-watchdog
```

If you use `cms-watchdog-util` from `/usr/local/bin`, symlink it rather
than copying it, so it can find `cms-watchdog.py`:
`sudo ln -s $CMS_HOME/scripts/cms-watchdog-util /usr/local/bin/`.

## Files

| What                     | Where (defaults)                                        |
|--------------------------|---------------------------------------------------------|
| Watchdog                 | `scripts/cms-watchdog.py`                               |
| Helper                   | `scripts/cms-watchdog-util`                             |
| systemd unit (sample)    | `config/cms-watchdog.service.sample`                    |
| Watchdog log             | `/var/local/log/cms/watchdog.log` (`--log-dir`)         |
| Ranking / proxy output   | `/var/local/log/cms/ranking.log`, `proxy.log`           |

The watchdog runs `scripts/cmsRankingWebServer` and
`scripts/cmsProxyService` from `--cms-home` when they exist there.
Otherwise it runs the installed commands from `PATH`. By default,
ProxyService runs in multi-contest mode (`-c ALL 0`). Use `-c <id>` to
serve a single contest.

## Manual control

When systemd stops the watchdog, the watchdog also stops the services it
started. If you want them to keep running, pass `--keep-services`.

```bash
sudo systemctl stop cms-watchdog      # stops watchdog + its services
cmsRankingWebServer &                 # run things by hand...
cmsProxyService -c ALL 0 &
sudo systemctl start cms-watchdog     # ...the watchdog adopts running healthy instances
```

## Ranking downtime and data loss

ProxyService no longer loses data while the ranking is unreachable.
Data that fails to send because of a network error, a 5xx response or a
timeout goes back into the queue and is retried every 60 seconds. Data
that the ranking rejects with a 4xx response is logged and dropped. The
ranking will never accept it, and keeping it would block everything
else. Users added while the contest is running are sent to the ranking
the first time one of their submissions is scored.

## Troubleshooting

- **A service keeps restarting.** Run `cms-watchdog-util service-logs`
  and look at the service's own output for the root cause.
- **"ResourceService is not reachable".** Start it with
  `cmsResourceService -a <contest_id>`, or fix its address in
  `cms.conf`.
- **"running outside the watchdog but not responding".** An instance
  that the watchdog did not start is hung. Kill it by hand, and the
  watchdog will start a fresh one.
- **Ranking check fails but the ranking works.** Check that
  `--ranking-url`, or the first `rankings` URL in `cms.conf`, points at
  the ranking server's real HTTP address.
