#!/usr/bin/env python3
"""
CMS Ranking/Proxy Watchdog - Auto-restart and health monitoring

This script monitors cmsRankingWebServer and cmsProxyService, automatically
restarting them if they crash while respecting ResourceService availability.
Ensures historical submissions are backfilled on startup.
"""

import subprocess
import time
import sys
import os
import signal
import json
import urllib.request
import urllib.error
import logging
from datetime import datetime
from pathlib import Path

# Configuration
CMS_HOME = "/home/arazoglu/cms"
LOG_FILE = "/tmp/cms-watchdog.log"
RESOURCE_SERVICE_PORT = 8001
RANKING_SERVICE_PORT = 8890
CHECK_INTERVAL = 30  # seconds between health checks
RESTART_COOLDOWN = 5  # seconds to wait before restarting after crash
RESOURCE_STABILITY_WAIT = 10  # seconds to wait for ResourceService to stabilize

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class CMSWatchdog:
    def __init__(self):
        self.ranking_process = None
        self.proxy_process = None
        self.running = True
        self.last_restart_time = {}
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, sig, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {sig}, shutting down watchdog...")
        self.running = False
        self._cleanup()
        sys.exit(0)

    def _cleanup(self):
        """Clean up processes on shutdown"""
        for process, name in [(self.ranking_process, "ranking"), (self.proxy_process, "proxy")]:
            if process and process.poll() is None:
                logger.info(f"Terminating {name} process (PID {process.pid})")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force killing {name} process")
                    process.kill()

    def is_resource_service_healthy(self):
        """Check if ResourceService is running and responsive"""
        try:
            response = urllib.request.urlopen(
                f'http://127.0.0.1:{RESOURCE_SERVICE_PORT}/ping',
                timeout=2
            )
            return response.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, Exception):
            return False

    def is_ranking_service_healthy(self):
        """Check if RankingWebServer is running and responsive"""
        try:
            response = urllib.request.urlopen(
                f'http://127.0.0.1:{RANKING_SERVICE_PORT}/contests/',
                timeout=2
            )
            return response.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, Exception):
            return False

    def is_proxy_service_running(self):
        """Check if ProxyService process is alive"""
        if self.proxy_process and self.proxy_process.poll() is None:
            return True
        return False

    def start_ranking_service(self):
        """Start cmsRankingWebServer"""
        if self.ranking_process and self.ranking_process.poll() is None:
            logger.debug("Ranking service already running")
            return True

        logger.info("Starting cmsRankingWebServer...")
        try:
            self.ranking_process = subprocess.Popen(
                ["cmsRankingWebServer"],
                cwd=CMS_HOME,
                stdout=open("/tmp/cms-ranking.log", "a"),
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid
            )
            logger.info(f"Started ranking service (PID {self.ranking_process.pid})")
            time.sleep(2)
            return True
        except Exception as e:
            logger.error(f"Failed to start ranking service: {e}")
            return False

    def start_proxy_service(self):
        """Start cmsProxyService in multicontest mode"""
        if self.proxy_process and self.proxy_process.poll() is None:
            logger.debug("Proxy service already running")
            return True

        logger.info("Starting cmsProxyService (multicontest mode)...")
        try:
            self.proxy_process = subprocess.Popen(
                ["scripts/cmsProxyService", "-c", "ALL", "0"],
                cwd=CMS_HOME,
                stdout=open("/tmp/cms-proxy.log", "a"),
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid
            )
            logger.info(f"Started proxy service (PID {self.proxy_process.pid})")
            logger.info("Proxy initialized: missed-operations replay will backfill submissions")
            time.sleep(3)
            return True
        except Exception as e:
            logger.error(f"Failed to start proxy service: {e}")
            return False

    def restart_services(self):
        """Restart ranking and proxy services"""
        # Check if ResourceService is healthy first
        if not self.is_resource_service_healthy():
            logger.warning("ResourceService not healthy yet, waiting before restart...")
            for i in range(RESOURCE_STABILITY_WAIT):
                if self.is_resource_service_healthy():
                    logger.info("ResourceService is now healthy")
                    break
                time.sleep(1)
            else:
                logger.error("ResourceService did not stabilize, skipping restart")
                return False

        # Give ResourceService a moment to be fully ready
        time.sleep(1)

        # Restart in order: ranking first (data sink), then proxy (data source)
        success = True
        if not self.start_ranking_service():
            success = False
        if not self.start_proxy_service():
            success = False

        if success:
            logger.info("✓ Both services restarted successfully")
        else:
            logger.warning("⚠ Restart partially failed, will retry at next check")

        return success

    def check_and_recover(self):
        """Check service health and recover if needed"""
        ranking_healthy = self.is_ranking_service_healthy()
        proxy_alive = self.is_proxy_service_running()

        status = []
        if ranking_healthy:
            status.append("ranking:✓")
        else:
            status.append("ranking:✗")

        if proxy_alive:
            status.append("proxy:✓")
        else:
            status.append("proxy:✗")

        logger.info(f"Health check: {' | '.join(status)}")

        # Determine what needs recovery
        needs_recovery = not ranking_healthy or not proxy_alive

        if needs_recovery:
            # Check cooldown
            now = time.time()
            last_restart = self.last_restart_time.get('services', 0)
            time_since_restart = now - last_restart

            if time_since_restart < RESTART_COOLDOWN:
                wait_time = RESTART_COOLDOWN - time_since_restart
                logger.info(f"Recent restart detected, waiting {wait_time:.1f}s before retry...")
                return

            logger.warning(f"Service failure detected: ranking={ranking_healthy}, proxy={proxy_alive}")
            if self.restart_services():
                self.last_restart_time['services'] = now
        else:
            logger.debug("All services healthy")

    def run(self):
        """Main watchdog loop"""
        logger.info("=" * 60)
        logger.info("CMS Watchdog started")
        logger.info("Monitoring: cmsRankingWebServer + cmsProxyService (-c ALL 0)")
        logger.info(f"Check interval: {CHECK_INTERVAL}s")
        logger.info(f"ResourceService port: {RESOURCE_SERVICE_PORT}")
        logger.info(f"Ranking service port: {RANKING_SERVICE_PORT}")
        logger.info("=" * 60)

        # Initial startup of all services
        if not self.is_resource_service_healthy():
            logger.warning("ResourceService not available at startup, waiting...")
            for i in range(30):
                if self.is_resource_service_healthy():
                    logger.info("ResourceService is now available")
                    break
                time.sleep(1)
            else:
                logger.error("ResourceService not available after 30s, cannot proceed")
                return

        logger.info("Starting services...")
        self.restart_services()

        # Main monitoring loop
        while self.running:
            try:
                time.sleep(CHECK_INTERVAL)
                self.check_and_recover()
            except KeyboardInterrupt:
                logger.info("Watchdog interrupted, shutting down...")
                break
            except Exception as e:
                logger.error(f"Unexpected error in watchdog loop: {e}", exc_info=True)
                time.sleep(CHECK_INTERVAL)

        self._cleanup()
        logger.info("CMS Watchdog stopped")


if __name__ == "__main__":
    watchdog = CMSWatchdog()
    watchdog.run()
