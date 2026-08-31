#!/bin/bash
# Cost-reduction-sprint pipeline advancer — script-only, silent on success.
# Dispatches ready tasks on the board when worker slots free up.
BOARD="cost-reduction-sprint"
LOGTAG="crs-advancer"

# Exit if board finished (all 4 done/review/archived)
STATUSES=$(export HERMES_URGENCY_EXEMPT=1; hermes kanban --board "$BOARD" ls --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    done=sum(1 for t in d if t['status'] in ('done','review','archived'))
    other=sum(1 for t in d if t['status'] not in ('done','review','archived'))
    print(f'{done} {other}')
except Exception:
    print('ERR ERR')
")
read -r DONE OTHER <<< "$STATUSES"
if [ "$DONE" = "ERR" ]; then
  echo "[$LOGTAG] WARN: could not read board"
  exit 0
fi
if [ "$OTHER" = "0" ] && [ "$DONE" != "0" ]; then
  echo "[$LOGTAG] BOARD COMPLETE: all tasks done/review. Remove this cron (job name: cost-reduction-sprint pipeline advancer)."
  exit 0
fi

# Dispatch ready tasks (dispatcher enforces per-profile cap 2)
export HERMES_URGENCY_EXEMPT=1
OUT=$(hermes kanban --board "$BOARD" dispatch --max 2 --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    spawned=d.get('spawned',[])
    print(len(spawned))
    for s in spawned:
        print(s.get('task_id','?'))
except Exception as e:
    print('ERR')
")
if [ "$OUT" != "0" ] && [ -n "$OUT" ]; then
  echo "[$LOGTAG] dispatched:"
  echo "$OUT"
fi
exit 0