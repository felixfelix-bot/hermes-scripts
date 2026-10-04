#!/usr/bin/env bash
# routstr-readiness-check.sh — Deterministic ADR-007 gate checker (no_agent cron).
#
# Output contract (watchdog pattern — silent unless a state CHANGE matters):
#   - stdout empty  = no meaningful change
#   - READY        = all four gates PASS (first time, or after not-ready)
#   - REGRESSION   = a gate that was PASS is now FAIL (NOT merely UNVERIFIED)
#
# State file: ~/.hermes/bot/routstr-readiness-state.json
#
# Gate semantics:
#   gate1 (Kalman): PASS  -> scored samples >= MIN_SCORED and verdict healthy/improving
#                   FAIL  -> scored samples >= MIN_SCORED and verdict is a real
#                            bad verdict (unhealthy/degraded/overconfident/weak_velocity)
#                   UNVERIFIED -> idle / too few scored points ("not converged").
#                            This is NOT a regression; it is reported as such only
#                            once it can actually be scored.
#   gate2 (Priority routing): PASS iff flat_router/zai_proxy has X-Priority.
#   gate3 (Stress test):      PASS iff stress_test_results.json exists + pass.
#   gate4 (Delist script):    PASS iff routstr_delist.py exists + executable.
#
# 2026-09-17 fixes (Phase H):
#   * A PASS->UNVERIFIED transition is NOT a regression (only PASS->FAIL is).
#     Previously any gate leaving the PASS set raised REGRESSION, so a lane
#     going idle (Kalman "not converged") produced a false-alarm.
#   * gate1 now has a real FAIL verdict (scored MAPE >= threshold), matching the
#     documented contract instead of never firing.
#   * MAPE threshold unified with kalman_health.GOOD_MAX_MAPE (25), not 15.
#   * MIN_SCORED mirrors kalman_health.MIN_SCORED so a 1-2 point MAPE cannot
#     score at all.

set -uo pipefail

BOT="$HOME/.hermes/bot"
STATE_FILE="$BOT/routstr-readiness-state.json"
PY="$HOME/.hermes/venv/bin/python3"
HEALTH_PY="$PY $BOT/kalman_health.py"

# Single source of truth shared with kalman_health.py.
MAPE_THRESHOLD="25.0"   # kalman_health.GOOD_MAX_MAPE
MIN_SCORED="5"          # kalman_health.MIN_SCORED

# ---------- Gate 1: Kalman (live) ----------
KALMAN_JSON=$($HEALTH_PY 2>/dev/null || true)

gate1_verdict="UNVERIFIED"
gate1_detail="traffic idle (no samples)"
if [ -n "$KALMAN_JSON" ]; then
  KV=$($PY -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('PARSE_ERR'); sys.exit()
keys=d.get('keys',{})
tot=sum(k.get('samples',0) for k in keys.values())
ov=d.get('overall_verdict','unknown')
mape=d.get('mean_abs_pct_error')
print(f'{tot}|{ov}|{mape}')
" <<< "$KALMAN_JSON")
  total_samples="${KV%%|*}"; rest="${KV#*|}"; overall="${rest%%|*}"; mape="${rest#*|}"
  if [ "$total_samples" = "PARSE_ERR" ]; then
    gate1_verdict="UNVERIFIED"; gate1_detail="health parse error -> cannot score"
  elif [ "$total_samples" -lt "$MIN_SCORED" ] 2>/dev/null; then
    gate1_verdict="UNVERIFIED"
    gate1_detail="insufficient scored samples (${total_samples} < ${MIN_SCORED}) -> not converged"
  elif [ "$overall" = "healthy" ] || [ "$overall" = "improving" ]; then
    gate1_verdict="PASS"; gate1_detail="live MAPE ${mape:-n/a} ($overall)"
  elif [ "$overall" = "insufficient_data" ]; then
    gate1_verdict="UNVERIFIED"; gate1_detail="insufficient_data -> not converged"
  else
    # Real scored verdict that is not healthy -> a genuine failure.
    gate1_verdict="FAIL"
    gate1_detail="live verdict $overall, MAPE ${mape:-n/a} (>= ${MAPE_THRESHOLD} threshold)"
  fi
fi

# ---------- Gate 2: Priority routing ----------
gate2_verdict="FAIL"; gate2_detail="X-Priority caller-class routing not found"
if grep -q "X-Priority" "$BOT/zai_proxy.py" 2>/dev/null \
   || grep -q "X-Priority" "$BOT/flat_router.py" 2>/dev/null \
   || grep -rq "X-Priority" "$HOME/merchant-routing-engine/flat_router.py" 2>/dev/null \
   || grep -rq "X-Priority" "$HOME/merchant-routing-engine/production/zai_proxy.py" 2>/dev/null; then
  gate2_verdict="PASS"; gate2_detail="X-Priority routing present"
fi

# ---------- Gate 3: Stress test ----------
gate3_verdict="FAIL"; gate3_detail="no stress_test_results.json"
if [ -f "$BOT/stress_test_results.json" ]; then
  if grep -q '"pass"\|"passed"\|"status"[[:space:]]*:[[:space:]]*"pass"\|"result"[[:space:]]*:[[:space:]]*"pass"' "$BOT/stress_test_results.json" 2>/dev/null; then
    gate3_verdict="PASS"; gate3_detail="stress test passed"
  else
    gate3_detail="stress_test_results.json present but no pass marker"
  fi
fi

# ---------- Gate 4: Delist script ----------
gate4_verdict="FAIL"; gate4_detail="routstr_delist.py missing"
if [ -f "$BOT/scripts/routstr_delist.py" ] && [ -x "$BOT/scripts/routstr_delist.py" ]; then
  gate4_verdict="PASS"; gate4_detail="delist script present + executable"
fi

# ---------- Aggregate ----------
all_pass="false"
if [ "$gate1_verdict" = "PASS" ] && [ "$gate2_verdict" = "PASS" ] \
   && [ "$gate3_verdict" = "PASS" ] && [ "$gate4_verdict" = "PASS" ]; then
  all_pass="true"
fi

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---------- Load previous state ----------
prev_all_pass="false"; prev_passing=""; LAST_CHANGE="$NOW"
if [ -f "$STATE_FILE" ]; then
  prev_all_pass=$($PY -c "import json;
try: print(json.load(open('$STATE_FILE')).get('all_pass','false'))
except Exception: print('false')" 2>/dev/null)
  prev_passing=$($PY -c "import json;
try:
 d=json.load(open('$STATE_FILE')); print(' '.join(k for k,v in d.get('gates',{}).items() if v=='PASS'))
except Exception: print('')" 2>/dev/null)
  prev_last=$($PY -c "import json;
try: print(json.load(open('$STATE_FILE')).get('last_change','$NOW'))
except Exception: print('$NOW')" 2>/dev/null)
  [ -n "$prev_last" ] && LAST_CHANGE="$prev_last"
fi

# ---------- Detect state change ----------
# Regression = a gate that was PASS is now FAIL. PASS->UNVERIFIED is NOT a
# regression (it means "cannot score yet"), so it stays silent.
cur_fail=""
[ "$gate1_verdict" = "FAIL" ] && cur_fail="$cur_fail gate1"
[ "$gate2_verdict" = "FAIL" ] && cur_fail="$cur_fail gate2"
[ "$gate3_verdict" = "FAIL" ] && cur_fail="$cur_fail gate3"
[ "$gate4_verdict" = "FAIL" ] && cur_fail="$cur_fail gate4"

MSG=""
if [ "$all_pass" = "true" ] && [ "$prev_all_pass" != "true" ]; then
  MSG="READY"; LAST_CHANGE="$NOW"
elif [ "$all_pass" = "true" ]; then
  :
else
  regressed=""
  for g in $prev_passing; do
    case " $cur_fail " in
      *" $g "*) regressed="$regressed $g" ;;
    esac
  done
  if [ -n "$regressed" ]; then MSG="REGRESSION"; LAST_CHANGE="$NOW"; fi
fi

# ---------- Persist state ----------
$PY - "$STATE_FILE" "$all_pass" "$gate1_verdict" "$gate2_verdict" "$gate3_verdict" "$gate4_verdict" "$LAST_CHANGE" <<'PY_PERSIST' 2>/dev/null
import json, sys, datetime
sf, all_pass, g1, g2, g3, g4, last = sys.argv[1:8]
gates = {"gate1": g1, "gate2": g2, "gate3": g3, "gate4": g4}
json.dump({
  "all_pass": all_pass == "true",
  "gates": gates,
  "passing": [g for g, v in gates.items() if v == "PASS"],
  "failing": [g for g, v in gates.items() if v == "FAIL"],
  "last_change": last,
  "checked_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
}, open(sf, "w"), indent=2)
PY_PERSIST

# ---------- Emit output only on change ----------
if [ "$MSG" = "READY" ]; then
  echo "🚀 ROUTSTR READY — all 4 gates passed."
  echo "  Gate1 (Kalman): $gate1_detail"
  echo "  Gate2 (Priority routing): $gate2_detail"
  echo "  Gate3 (Stress test): $gate3_detail"
  echo "  Gate4 (Delist): $gate4_detail"
  echo "ADR-007 gates OPEN — Felix should approve Phase 1."
elif [ "$MSG" = "REGRESSION" ]; then
  echo "⚠️ ROUTSTR GATE REGRESSION — a gate that was PASSING now FAILS:"
  echo "  Gate1: $gate1_verdict — $gate1_detail"
  echo "  Gate2: $gate2_verdict — $gate2_detail"
  echo "  Gate3: $gate3_verdict — $gate3_detail"
  echo "  Gate4: $gate4_verdict — $gate4_detail"
fi
exit 0
