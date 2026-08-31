#!/bin/bash
# v0.6.0 release watchdog (tollgate-module-basic-go)
# Releases gated kanban tasks as upstream events land. Empty stdout = silent.
# Phase 1: upstream #369+#370 merged -> unblock T1 (changelog) + T2 (race tests)
# Phase 2: T1+T2 done -> unblock T3 (tag prep)
# Phase 3: T3 done -> one final notify. Cron */30 * * * *, no_agent.
BOARD=tollgate-module-basic-go
T1=t_7bee945b; T2=t_d9378b1b; T3=t_8e133184
STATE="$HOME/.hermes/bot/v060_watchdog_state.json"
HERMES=$(command -v hermes || echo /home/c03rad0r/.local/bin/hermes)

st() { python3 -c "import json;print(json.load(open('$STATE')).get('$1',''))" 2>/dev/null; }
setst() { python3 - "$1" "$2" "$STATE" <<'PYEOF'
import json, os, sys
k, v, p = sys.argv[1], sys.argv[2], sys.argv[3]
d = {}
try: d = json.load(open(p))
except Exception: pass
d[k] = v if not v.isdigit() else int(v)
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w')); os.replace(tmp, p)
PYEOF
}

prs_merged() {
  for n in 369 370; do
    s=$(curl -sf --max-time 15 -H 'User-Agent: v060-watchdog' \
      "https://api.github.com/repos/OpenTollGate/tollgate-module-basic-go/pulls/$n" 2>/dev/null | \
      python3 -c 'import sys,json;print(json.load(sys.stdin).get("merged",False))' 2>/dev/null)
    [ "$s" = "True" ] || return 1
  done
  return 0
}

task_status() {
  $HERMES kanban --board "$BOARD" list --json 2>/dev/null | python3 -c "
import sys, json
tid = '$1'
d = json.load(sys.stdin)
tasks = d if isinstance(d, list) else d.get('tasks', [])
for t in tasks:
    if t.get('id') == tid:
        print(t.get('status', '')); break
" 2>/dev/null
}

is_done() { case "$1" in done|completed|archived) return 0;; *) return 1;; esac; }

if [ "$(st phase1)" != "1" ]; then
  if prs_merged; then
    "$HERMES" kanban --board "$BOARD" unblock "$T1" --reason 'watchdog: upstream #369+#370 merged — v0.6.0 steps 2+3 released' >/dev/null 2>&1
    "$HERMES" kanban --board "$BOARD" unblock "$T2" --reason 'watchdog: upstream #369+#370 merged — v0.6.0 steps 2+3 released' >/dev/null 2>&1
    setst phase1 1
    echo "V0.6.0 PIPELINE RELEASED: #369+#370 merged upstream. Unblocked CHANGELOG-BACKFILL-V060 + RACE-TEST-V060 (board $BOARD). Tag-prep follows automatically; tag push stays Felix-manual."
  fi
  exit 0
fi

if [ "$(st phase2)" != "1" ]; then
  if is_done "$(task_status "$T1")" && is_done "$(task_status "$T2")"; then
    "$HERMES" kanban --board "$BOARD" unblock "$T3" --reason 'watchdog: changelog + race gates done — tag prep released' >/dev/null 2>&1
    setst phase2 1
    echo "V0.6.0 TAG-PREP RELEASED: changelog + race gates completed. TAG-PREP-V060 dispatched; release notes + tag checklist land on issue #339. Tag push = Felix manual only."
  fi
  exit 0
fi

if [ "$(st phase3)" != "1" ]; then
  if is_done "$(task_status "$T3")"; then
    setst phase3 1
    echo "V0.6.0 PIPELINE COMPLETE: release notes + maintainer tag checklist are on issue #339. Final step: Felix tags v0.6.0 manually (one-shot Nostr-wide publish — use SHA from checklist, never push --tags)."
  fi
  exit 0
fi

exit 0
