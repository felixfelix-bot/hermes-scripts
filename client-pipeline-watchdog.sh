#!/bin/bash
# Generic DEFER-release watchdog for a client pipeline kanban board.
# The board slug, task IDs, and not-before date live in a PRIVATE env file
# (sourced below) so this repo never carries client-identifying values.
# Script-only: zero LLM tokens. Pressure-gated, self-cleaning, silent idle.
export PATH="$HOME/.hermes/hermes-agent/venv/bin:$PATH"

CFG="$HOME/.hermes/profiles/manager/state/client-pipeline.env"
[ -f "$CFG" ] && . "$CFG"
BOARD="${CLIENT_BOARD:?missing config: $CFG}"
TASKS="${CLIENT_TASKS:?missing config: $CFG}"
NB="${CLIENT_NB:-2026-09-07}"

[ "$(date +%F)" \< "$NB" ] && exit 0

GATE="$HOME/.hermes/profiles/manager/scripts/zai-quota-gate.sh"
[ -x "$GATE" ] && "$GATE" >/dev/null 2>&1 || exit 0

read W F <<<"$(curl -sf --connect-timeout 5 http://localhost:9099/quota 2>/dev/null | python3 -c "
import sys, json
w = f = 100
try:
    d = json.load(sys.stdin)
    for win in ((d.get('ours') or {}).get('windows') or []):
        n = (win.get('name') or '').lower()
        u = win.get('used_pct', 100)
        if isinstance(u, (int, float)):
            v = int(round(u))
            if 'week' in n: w = v
            if '5-hour' in n or '5h' in n: f = v
except Exception:
    pass
print(w, f)")"
awk -v w="$W" -v f="$F" 'BEGIN{exit !(w<60 && f<40)}' || exit 0

STATUS_OF='import sys,json; d=json.load(sys.stdin); m=[x for x in d if x["id"]==sys.argv[1]]; print(m[0]["status"] if m else "gone")'
DONECT='import sys,json; d=json.load(sys.stdin); print(sum(1 for x in d if x.get("status") not in ("done","cancelled","archived")))'

ALL=$(hermes kanban --board "$BOARD" list --json 2>/dev/null)
[ -n "$ALL" ] || exit 0
LEFT=$(echo "$ALL" | python3 -c "$DONECT")
if [ "$LEFT" = "0" ]; then
  crontab -l 2>/dev/null | grep -v 'client-pipeline-watchdog' | crontab - 2>/dev/null
  echo "client pipeline complete; watchdog removed."
  exit 0
fi

for id in $TASKS; do
  S=$(echo "$ALL" | python3 -c "$STATUS_OF" "$id")
  if [ "$S" = "blocked" ]; then
    hermes kanban --board "$BOARD" unblock "$id" "pressure window open (weekly=$W%, 5h=$F%)" >/dev/null 2>&1
  fi
done

D=$(hermes kanban --board "$BOARD" dispatch --max 1 2>&1)
if ! grep -qE 'Spawned:[[:space:]]+0$' <<<"$D"; then
  echo "Dispatched client pipeline (weekly=$W%, 5h=$F%): $D"
fi
exit 0
