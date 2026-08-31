#!/usr/bin/env bash
# vps51-watchdog.sh — revival watchdog for the 2026-08-28 AS401933 outage.
# Every 5 min via cron (no_agent). SILENT while dark (empty stdout).
# On confirmed revival: alert (stdout → Signal group) + unblock staged kanban tasks.
# On fall-after-alive: alert once. Task RO-1 generalizes this into a full uptime monitor.
set -u
IP=23.182.128.51
IP2=23.182.128.219
STATE_FILE="$HOME/.hermes/profiles/manager/cron/state/vps51-watchdog.state"
BOARD=merchant-module
STAGED_TASKS=(t_9c25b7d9 t_5f69c815)   # RO-2, RO-4
CONFIRM=2   # consecutive passes required to declare revival

now=$(date -u +%s)
probe_ok=0
nc -zw5 "$IP" 22 >/dev/null 2>&1 && probe_ok=1
tls=""
[ "$probe_ok" = 1 ] && tls=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' "https://routstr.orangesync.tech/v1/info" 2>/dev/null)

# load/init state
status=dark; streak=0; changed=0
if [ -f "$STATE_FILE" ]; then
    read -r status streak changed <<<"$(python3 -c "
import json
try:
    d=json.load(open('$STATE_FILE'))
    print(d.get('status','dark'), d.get('streak',0), d.get('changed',0))
except Exception:
    print('dark',0,0)
" 2>/dev/null)"
fi

out=""
if [ "$probe_ok" = 1 ]; then
    streak=$((streak+1))
    if [ "$status" != "alive" ] && [ "$streak" -ge "$CONFIRM" ]; then
        status=alive
        out="ALERT: VPS2 23.182.128.51 IS BACK (TCP/22 reachable, TLS=$tls). Revival confirmed after AS401933 outage (dark since Aug 28 ~21:30Z). Unblocking recovery tasks RO-2/RO-4 now — recovery worker will start automatically."
        for t in "${STAGED_TASKS[@]}"; do
            hermes kanban --board "$BOARD" unblock "$t" >/dev/null 2>&1 && out="$out
unblocked: $t"
        done
        hermes kanban --board "$BOARD" comment t_9c25b7d9 "Watchdog: box revived at $(date -u '+%F %TZ'). Begin 8-step gated recovery." >/dev/null 2>&1
    fi
else
    if [ "$status" = "alive" ]; then
        out="ALERT: 23.182.128.51 went DARK AGAIN (was alive at last check, changed=$changed). Possible provider instability — verify before trusting recovery."
        status=dark
    fi
    streak=0
fi

python3 -c "
import json
json.dump({'status':'$status','streak':$streak,'changed':$now,'ip2_ok':0}, open('$STATE_FILE','w'))
" 2>/dev/null

# silence = healthy-dark or stable; speak only on transitions
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
