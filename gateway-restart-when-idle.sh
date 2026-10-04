#!/usr/bin/env bash
# gateway-restart-when-idle.sh — apply gateway config changes without killing
# in-flight kanban work.
#
# The gateway spawns the MCP servers and hosts the cron scheduler; restarting it
# while workers run kills their child processes. This restarts the gateway ONLY
# when no kanban worker is running, and self-disables once the pending config
# change is in effect.
#
# Trigger (2026-09-17): stop the dq05_monitor MCP (config set enabled:false; the
# running gateway keeps respawning it until it reloads).
#
# Run via systemd timer (hermes-gateway-idle-restart.timer, every 5m).
# Always exits 0; "deferred" is a normal state, not a failure.
set -uo pipefail

# Already applied? (gateway no longer spawns it)
if ! pgrep -f "dq05_monitor_mcp" >/dev/null 2>&1; then
  exit 0
fi

# Count running kanban workers across all boards in ONE python pass.
running=$(python3 - <<'PY' 2>/dev/null
import glob, os, sqlite3
n = 0
for db in glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")):
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n += int(c.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0])
        c.close()
    except Exception:
        pass
print(n)
PY
)

if [ "${running:-0}" -gt 0 ]; then
  echo "gateway-idle-restart: deferred — ${running} worker(s) running"
  exit 0
fi

systemctl --user restart hermes-gateway.service && \
  echo "gateway-idle-restart: gateway restarted while idle (applied config)"
exit 0
