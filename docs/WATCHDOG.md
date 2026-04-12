# CMS Ranking/Proxy Watchdog

Automated monitoring and recovery system for the CMS ranking and proxy services.

## What It Does

The watchdog daemon (`cms-watchdog.py`) automatically:

1. **Monitors service health** - Checks every 30 seconds if cmsRankingWebServer and cmsProxyService are running
2. **Auto-restarts on crash** - If either service dies, the watchdog restarts it immediately
3. **Respects ResourceService** - Never starts ranking/proxy until ResourceService is healthy (never interferes)
4. **Backfills submissions** - When ProxyService starts, it automatically replays missed operations to restore historical submissions
5. **Logs everything** - All actions logged to `/tmp/cms-watchdog.log` and systemd journal
6. **Runs as a daemon** - Installed as systemd service that auto-starts on boot

## Quick Commands

```bash
# Check status
cms-watchdog-util status

# View recent activity
cms-watchdog-util logs

# Follow live logs
cms-watchdog-util follow

# Restart watchdog if needed
cms-watchdog-util restart

# Enable/disable auto-start on boot
cms-watchdog-util enable
cms-watchdog-util disable
```

## How It Works

### Startup Sequence
1. Watchdog starts (systemd service)
2. Waits for ResourceService to be healthy on port 8001
3. Once ResourceService ready, starts cmsRankingWebServer
4. Starts cmsProxyService with multicontest mode (`-c ALL 0`)
   - ProxyService detects missed operations from before it was running
   - Automatically replays them to backfill submissions
5. Enters monitoring loop, checking every 30 seconds

### Recovery Process
When a service crash is detected:
1. Waits 5 seconds (cooldown period to avoid restart loops)
2. Checks that ResourceService is still healthy
3. Restarts ranking service first, then proxy service
4. Proxy backfills any missed operations that occurred during downtime
5. Resumes monitoring

### Health Checks
- **Ranking**: HTTP GET to `http://127.0.0.1:8890/contests/` (must return HTTP 200)
- **Proxy**: Checks if process is running in background
- **ResourceService**: HTTP GET to `http://127.0.0.1:8001/ping` (must return HTTP 200)

## Files

- **Watchdog script**: `/home/arazoglu/cms/scripts/cms-watchdog.py` (Python)
- **Systemd service**: `/etc/systemd/system/cms-watchdog.service`
- **Watchdog logs**: `/tmp/cms-watchdog.log` (main), systemd journal (via `journalctl -u cms-watchdog`)
- **Service logs**: `/tmp/cms-ranking.log`, `/tmp/cms-proxy.log`
- **Utility**: `/usr/local/bin/cms-watchdog-util` (bash)

## Logs

Check logs to understand what's happening:

```bash
# Last 20 lines of watchdog activity
tail -n 20 /tmp/cms-watchdog.log

# Full history
tail -n 100 /tmp/cms-watchdog.log

# Follow in real-time
cms-watchdog-util follow

# Via systemd (includes system context)
journalctl -u cms-watchdog.service -f
```

Example log output:
```
2026-04-12 08:07:43 [INFO] CMS Watchdog started
2026-04-12 08:07:43 [INFO] Monitoring: cmsRankingWebServer + cmsProxyService (-c ALL 0)
2026-04-12 08:07:50 [INFO] ResourceService is now available
2026-04-12 08:07:51 [INFO] Starting services...
2026-04-12 08:07:52 [INFO] Started ranking service (PID 4521)
2026-04-12 08:07:54 [INFO] Started proxy service (PID 4541)
2026-04-12 08:07:54 [INFO] Proxy initialized: missed-operations replay will backfill submissions
2026-04-12 08:07:54 [INFO] ✓ Both services restarted successfully
2026-04-12 08:08:24 [INFO] Health check: ranking:✓ | proxy:✓
```

## Manual Control

You can still manually stop/start services (watchdog won't interfere during 5-second cooldown):

```bash
# Stop watchdog (doesn't stop services, they keep running)
sudo systemctl stop cms-watchdog.service

# Stop services (watchdog will restart them if it's running)
pkill -f cmsRankingWebServer
pkill -f cmsProxyService

# Stop everything including watchdog
sudo systemctl stop cms-watchdog.service
pkill -f cmsRankingWebServer
pkill -f cmsProxyService
```

## ResourceService Safety

The watchdog **never** touches ResourceService:
- Only checks if it's healthy before restarting ranking/proxy
- If ResourceService is down, watchdog waits up to 30 seconds for it to recover
- If ResourceService isn't healthy, ranking/proxy restart is delayed
- This ensures you can work with ResourceService without the watchdog interfering

## Backfill Behavior

When ProxyService starts (including auto-restarts):
1. It connects to the database
2. Detects any operations that happened while it was offline
3. Automatically replays them to the ranking server
4. Logs: "Found X missed operation(s)"

This ensures historical submissions are always restored, even if:
- Ranking was down for maintenance
- Services crashed
- System was rebooted

## Troubleshooting

### Watchdog not running
```bash
sudo systemctl status cms-watchdog.service
```

### Services crash repeatedly
Check logs for root cause:
```bash
cms-watchdog-util logs
```

### Manually verify services are running
```bash
ps aux | grep -E 'cmsRanking|cmsProxy' | grep -v grep
```

### Check service responsiveness
```bash
curl http://127.0.0.1:8890/contests/
curl http://127.0.0.1:8001/ping
```

### Disable watchdog temporarily
```bash
sudo systemctl stop cms-watchdog.service
```

Then manually start services:
```bash
cd /home/arazoglu/cms && cmsRankingWebServer >/tmp/cms-ranking.log 2>&1 &
cd /home/arazoglu/cms && scripts/cmsProxyService -c ALL 0 >/tmp/cms-proxy.log 2>&1 &
```

### Re-enable watchdog
```bash
sudo systemctl start cms-watchdog.service
```

## Auto-Start on Boot

The watchdog is automatically configured to start on system boot:

```bash
# Verify it's enabled
sudo systemctl is-enabled cms-watchdog.service
# Output: enabled

# If you want to disable auto-start:
cms-watchdog-util disable

# To re-enable:
cms-watchdog-util enable
```

When the system reboots, the watchdog will automatically:
1. Wait for ResourceService to be ready
2. Start cmsRankingWebServer
3. Start cmsProxyService with backfill
4. Begin monitoring

No manual intervention needed.

## Summary

With this watchdog in place:
- ✅ Services automatically restart if they crash
- ✅ Historical submissions are backfilled on startup
- ✅ Monitoring happens continuously without user intervention
- ✅ ResourceService is never interfered with
- ✅ All activity is logged for troubleshooting
- ✅ System survives reboots (auto-starts on boot)

You can focus on running the contest while the watchdog handles service reliability.
