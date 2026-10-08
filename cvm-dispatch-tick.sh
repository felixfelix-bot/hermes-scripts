#!/usr/bin/env bash
# cvm-dispatch-tick.sh — keep the contextvm-services card chain moving.
# Silent (no output) when nothing is dispatchable or when a freeze is present.
# Prints one line per newly spawned task so the operator sees progress.
set -uo pipefail

export HOME=/home/c03rad0r
HERMES=/home/c03rad0r/.hermes/hermes-agent/venv/bin/hermes
BOARD=contextvm-services

# Respect every freeze marker: no dispatch when any is present.
for m in "$HOME/.hermes/bot/.dispatch_frozen" "$HOME/.hermes/ESTOP" "$HOME/.hermes/bot/.fleet_quarantine"; do
  [ -e "$m" ] && exit 0
done

# --- resource guard: never dispatch into a full fleet or a starved box -------
# At/over the fleet cap the spawn is GATED *after* the pid is registered, so the
# card is parked `blocked` with a fake "dead worker pid, strike 1" and the
# dispatcher will not retry it. Skipping the tick is strictly better: the card
# stays `ready` and the next tick picks it up once a slot frees.
CAP=${CVM_MAX_WRAPPERS:-4}
live=$(pgrep -fc kanban-crash-wrapper 2>/dev/null || echo 0)
[ "${live:-0}" -ge "$CAP" ] && exit 0

# Memory floor: below this the new worker starves the box and systemd-oomd kills
# it ~60s after spawn (wasted quota, no deliverable).
avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
[ "${avail:-0}" -lt "${CVM_MIN_AVAIL_MB:-2000}" ] && exit 0

# Cheap/quota admission gate (exit 1 => stay quiet).
gate_out=$("$HOME/.hermes/profiles/manager/scripts/dispatch-gate.sh" 2>&1) || exit 0
case "$gate_out" in
  *ALLOW*) : ;;
  *) exit 0 ;;
esac

out=$("$HERMES" kanban --board "$BOARD" dispatch --max 2 --json 2>/dev/null) || exit 0

echo "$out" | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sp = d.get("spawned") or []
if not sp:
    sys.exit(0)
for s in sp:
    tid = s.get("task_id") or s.get("id")
    who = s.get("assignee") or "?"
    print(f"CVM chain: spawned {tid} on {who}")
'
exit 0
