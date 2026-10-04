#!/bin/bash
# dispatch-gate.sh — admission gate for LLM-driven dispatch (renamed from
# zai-quota-gate.sh, Phase R: it is no longer zai-specific — it gates on the
# proxy's /v1/dispatch_gate, which covers every lane).
#
# Compatibility: invoked by pre-dispatch cron gates / watchdogs. The old name
# `zai-quota-gate.sh` still works via a deprecated shim.
#
# Policy (Felix 2026-08-24 "NO CAPS" directive, amended 2026-08-31):
#   Block dispatch ONLY when the proxy is unreachable AND no provider can serve.
#   If the proxy serves via ANY provider (z.ai friend, Ollama Cloud, NeuralWatt,
#   OpenCode Go, DeepSeek V4, PPQ), dispatch is ALLOWED — even with z.ai quota
#   at 100%. Abnormal burn = alerts only, never blocks.
#
# Block conditions (exit 1, reason on stderr):
#   frozen      — ~/.hermes/bot/.dispatch_frozen marker present
#   auth        — /v1/models returns "Authentication Failed"
#   proxy-down  — /v1/models unreachable / error body / HTTP 503
#   no-provider — friend locked (or dispatch_gate degraded) AND the fallback
#                 probe (deepseek-v4-flash) also fails: proxy alive but
#                 nothing can serve completions (fail-closed)
#
# Allow conditions (exit 0):
#   friend unlocked per /v1/dispatch_gate (quota_state.friend.locked == false)
#   OR fallback probe returns a valid completion from some non-z.ai provider
#
# Exit 0 = some provider can serve, OK to dispatch
# Exit 1 = frozen / auth / proxy-down / no-provider

set -euo pipefail

# Overridable proxy base (tests point this at a stub). The DISPATCH_GATE_* names
# are canonical; the ZAI_* names are honored for backward compatibility.
PROXY_URL="${DISPATCH_GATE_PROXY_URL:-${ZAI_PROXY_URL:-http://localhost:9099}}"
HOLD_LABEL="${DISPATCH_GATE_HOLD_LABEL:-${ZAI_HOLD_LABEL:-?}}"

# ── Phase 0: dispatch freeze marker ──
if [ -f "$HOME/.hermes/bot/.dispatch_frozen" ]; then
    echo "cheap-gate: BLOCK frozen" >&2
    exit 1
fi

# ── Phase 0b: daily token-burn cap (PLAN-cron-script-first, default-deny) ──
# Not a "no caps" gate any more: dispatch (which spawns LLM worker sessions) is
# blocked once the day's spend reaches the cap in state/fleet/burn_policy.json.
BURN_GATE="$HOME/hermes-orchestration/scripts/fleet/burn_gate.py"
if [ -f "$BURN_GATE" ]; then
    if ! python3 "$BURN_GATE" --kind dispatch --quiet; then
        echo "cheap-gate: BLOCK burn-cap" >&2
        exit 1
    fi
fi

# ── Phase 1: proxy liveness (provider-agnostic) via /v1/models ──
API_HEALTH=$(curl -s ${PROXY_URL}/v1/models | head -1 || echo "FAILED")

# Check if API is responding with authentication
if [[ "$API_HEALTH" == "Authentication Failed" ]]; then
    echo "cheap-gate: BLOCK auth" >&2
    exit 1
fi

# Check for any other API errors
if [[ "$API_HEALTH" == "FAILED" ]] || [[ "$API_HEALTH" == *"error"* ]]; then
    echo "cheap-gate: BLOCK proxy-down" >&2
    exit 1
fi

# HTTP status check (503 = proxy cannot serve)
QUOTA_STATUS=$(curl -s -w "%{http_code}" ${PROXY_URL}/v1/models -o /dev/null || echo "503")

if [[ "$QUOTA_STATUS" == "503" ]]; then
    echo "cheap-gate: BLOCK proxy-down" >&2
    exit 1
fi

# ── Phase 2: provider-capacity check via /v1/dispatch_gate ──
# Verdicts from the parser:
#   ALLOW    — quota_state.friend.locked == false → friend can serve
#   PROBE    — friend locked (or quota_state / friend field missing)
#   DEGRADED — curl failed or JSON unparseable → proxy degraded-but-alive
GATE_JSON=$(curl -s -m 10 ${PROXY_URL}/v1/dispatch_gate 2>/dev/null || echo "")

# Phase 2.0 (K1, 2026-09-17): CAPACITY gate. `can_dispatch == false` from the
# router means NO lane has remaining headroom (Phase-G capacity semantics).
# HOLD — dispatching into a drained pool just 503s the worker (reviews/crons),
# which is exactly the failure this gate exists to prevent. Fail-open on a
# parse hiccup (the Phase-3 probe still guards).
CAN_DISPATCH=$(echo "$GATE_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
print('true' if d.get('can_dispatch', True) else 'false')
" 2>/dev/null || echo unknown)
if [ "$CAN_DISPATCH" = "false" ]; then
    echo "cheap-gate: BLOCK capacity (no lane has remaining headroom)" >&2
    # K6: record the hold so the recovery digest can report what was held.
    # Callers may set DISPATCH_GATE_HOLD_LABEL (or legacy ZAI_HOLD_LABEL) to name the job/card.
    python3 -c "import json,time,os,sys;p=os.path.expanduser('~/.hermes/bot/.capacity_held.jsonl');open(p,'a').write(json.dumps({'ts':int(time.time()),'label':sys.argv[1]})+chr(10))" "$HOLD_LABEL" 2>/dev/null || true
    exit 1
fi

FRIEND_VERDICT=$(echo "$GATE_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('DEGRADED')  # unparseable JSON → fall back to live probe
    sys.exit(0)
qs = d.get('quota_state')
if not isinstance(qs, dict):
    print('PROBE')  # missing quota_state → treat like friend-locked
    sys.exit(0)
friend = qs.get('friend')
if not isinstance(friend, dict) or 'locked' not in friend:
    print('PROBE')  # friend field missing → treat like friend-locked
    sys.exit(0)
if not friend.get('locked'):
    print('ALLOW')  # friend unlocked → some provider can serve
    sys.exit(0)  # success path — outside try so bare-except can't swallow it
print('PROBE')  # friend locked → Phase 3 fallback probe
" 2>/dev/null || echo "DEGRADED")

case "$FRIEND_VERDICT" in
    ALLOW)
        echo "cheap-gate: ALLOW via friend" >&2
        exit 0
        ;;
    PROBE)
        :  # friend locked → fall through to Phase 3
        ;;
    *)
        echo "cheap-gate: dispatch_gate-degraded, falling back to live probe" >&2
        ;;
esac

# ── Phase 3: fallback live probe (non-z.ai capacity) ──
# Probing a z.ai model when z.ai is locked proves nothing about fallback
# capacity; deepseek-v4-flash routes via the live router to whatever provider
# is cheapest/available (Ollama Cloud, NeuralWatt, OpenCode Go, PPQ, ...).
PROBE_RESPONSE=$(curl -sf --connect-timeout 5 --max-time 45 \
    -X POST ${PROXY_URL}/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"OK"}],"max_tokens":1}' \
    2>/dev/null || echo "PROBE_FAILED")

if [ "$PROBE_RESPONSE" = "PROBE_FAILED" ]; then
    echo "cheap-gate: BLOCK no-provider" >&2
    exit 1  # proxy alive but no provider can serve completions (fail-closed)
fi

# Validate the response is a real completion (choices present, no error)
if echo "$PROBE_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)  # malformed response
if 'error' in d:
    sys.exit(1)
if 'choices' not in d:
    sys.exit(1)
sys.exit(0)  # OK — outside try so bare-except can't swallow it
" 2>/dev/null; then
    # Some non-z.ai provider served the probe → ALLOW
    echo "cheap-gate: ALLOW via fallback-probe" >&2
    exit 0
else
    echo "cheap-gate: BLOCK no-provider" >&2
    exit 1  # API returned error or malformed response
fi