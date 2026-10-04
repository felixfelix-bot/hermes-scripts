#!/bin/bash
# Continuous routstr stress-soak tick (ADR-007 Gate 3, T-C).
# Runs a bounded stress soak against the GATE-BEARING flat_router checkout
# (the worktree that carries flat_router.sold_429_gate from T-A) and rewrites
# ~/.hermes/bot/stress_test_results.json. The deployed ~/.hermes/bot
# flat_router does NOT yet carry the T-A gate, so we must run from the repo
# checkout or the gate fails open (false pass). 30 min per tick.
set -u
WORKTREE=/home/c03rad0r/worktrees/t_4090343e
OUT=~/.hermes/bot/stress_test_results.json
INTERVAL_S=1800
cd "$WORKTREE" || { echo "worktree missing"; exit 1; }
python3 stress_test.py --duration "$INTERVAL_S" --out "$OUT" >/dev/null 2>&1
rc=$?
# Confirm the results file carries a pass marker
python3 - "$OUT" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print("status="+d.get("status","?")+" internal_blocked="+str(d["detail"]["internal_blocked"])+" sold_429="+str(d["detail"]["sold_429"])+" sold_served="+str(d["detail"]["sold_served"])+" delist_latency="+str(d["detail"]["delist_latency_max_s"]))
except Exception as e:
    print("unreadable results:", e)
PY
exit $rc
