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
#                 probe also fails: proxy alive but nothing can serve
#                 completions (fail-closed)
#   no-lane     — DISPATCH_GATE_MODEL/--model set and the router declares no lane
#                 for that model (config fault: a retry can never fix it)
#   capacity    — DISPATCH_GATE_MODEL/--model set and the router answers
#                 "all providers exhausted" for that model (transient)
#
# Allow conditions (exit 0):
#   friend unlocked per /v1/dispatch_gate (quota_state.friend.locked == false)
#   OR a live 1-token completion succeeds for the probed model (default:
#      deepseek-v4-flash; with --model/DISPATCH_GATE_MODEL: THAT model, and the
#      model-agnostic K1 verdict does not override it)
#
# Exit 0 = some provider can serve, OK to dispatch
# Exit 1 = frozen / auth / proxy-down / no-provider

set -euo pipefail

# Overridable proxy base (tests point this at a stub). The DISPATCH_GATE_* names
# are canonical; the ZAI_* names are honored for backward compatibility.
PROXY_URL="${DISPATCH_GATE_PROXY_URL:-${ZAI_PROXY_URL:-http://localhost:9099}}"
HOLD_LABEL="${DISPATCH_GATE_HOLD_LABEL:-${ZAI_HOLD_LABEL:-?}}"

# ── Phase 0b: which model will the caller actually request? (2026-10-02) ──
# The gate MUST probe the model the dispatch is about to use. Probing a generic
# fallback proves only that SOMEONE can serve — a worker pinned to glm-5.2 dies
# on the very next call while the generic probe is green. Measured 2026-10-02:
# `deepseek-v4-flash` served 200 while `glm-5.2`/`glm-5.3`/`kimi-k2.7-code` all
# returned 503 "all providers exhausted", and a watchdog built on the gate
# dispatched straight into the dead pool.
#
#   DISPATCH_GATE_MODEL=<model>   (or `--model <model>`)
#
# Unset => legacy behaviour (generic deepseek-v4-flash probe), so existing
# callers are unaffected. With a model set, the failure is CLASSIFIED:
#   no-lane  — "no lane declares this model": a config fault, no retry can fix it
#   capacity — "all providers exhausted": transient, retry with backoff
#   no-provider / unknown — anything else (fail closed)
PROBE_MODEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --model) PROBE_MODEL="${2:-}"; shift 2 ;;
        --model=*) PROBE_MODEL="${1#--model=}"; shift ;;
        *) shift ;;
    esac
done
PROBE_MODEL="${DISPATCH_GATE_MODEL:-$PROBE_MODEL}"
if [ -z "$PROBE_MODEL" ]; then PROBE_GENERIC=1; PROBE_MODEL="deepseek-v4-flash"; else PROBE_GENERIC=0; fi

# ── Phase 0: dispatch freeze marker ──
if [ -f "$HOME/.hermes/bot/.dispatch_frozen" ]; then
    echo "cheap-gate: BLOCK frozen" >&2
    exit 1
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
if [ "$CAN_DISPATCH" = "false" ] && [ "$PROBE_GENERIC" = "1" ]; then
    echo "cheap-gate: BLOCK capacity (no lane has remaining headroom)" >&2
    # K6: record the hold so the recovery digest can report what was held.
    # Callers may set DISPATCH_GATE_HOLD_LABEL (or legacy ZAI_HOLD_LABEL) to name the job/card.
    python3 -c "import json,time,os,sys;p=os.path.expanduser('~/.hermes/bot/.capacity_held.jsonl');open(p,'a').write(json.dumps({'ts':int(time.time()),'label':sys.argv[1]})+chr(10))" "$HOLD_LABEL" 2>/dev/null || true
    exit 1
fi
# Model-pinned callers do NOT take the K1 verdict as final (2026-10-02): K1 is
# model-agnostic ("no lane has headroom" anywhere), so it blocks dispatches for
# models that verifiably serve. Ground truth for a pinned model is the Phase-3
# probe of THAT model, below. K1 still hard-blocks the generic path.

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
# capacity; the probe model routes via the live router to whatever provider is
# cheapest/available (Ollama Cloud, NeuralWatt, OpenCode Go, PPQ, ...).
#
# 2026-10-02: the probe targets the model the caller will actually request
# ($PROBE_MODEL) and the failure is classified, instead of collapsing every
# non-200 into "no-provider". `curl -f` used to discard the 503 body, which is
# where the classification lives.
PROBE_BODY_FILE="$(mktemp -t dispatch-gate-probe.XXXXXX)"
PROBE_CODE=$(curl -s -o "$PROBE_BODY_FILE" -w '%{http_code}' --connect-timeout 5 --max-time 45 \
    -X POST ${PROXY_URL}/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${PROBE_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"OK\"}],\"max_tokens\":1}" \
    2>/dev/null || echo "000")
[ -n "$PROBE_CODE" ] || PROBE_CODE="000"

# Classify. Exit 0 => allowed, 1 => blocked. `reason` is printed on stderr.
VERDICT=$(PROBE_CODE="$PROBE_CODE" PROBE_MODEL="$PROBE_MODEL" PROBE_BODY_FILE="$PROBE_BODY_FILE" python3 -c "
import json, os
code = os.environ.get('PROBE_CODE', '000')
model = os.environ.get('PROBE_MODEL', '?')
try:
    raw = open(os.environ['PROBE_BODY_FILE'], 'r', errors='replace').read()
except Exception:
    raw = ''
body = raw.strip()
low = body.lower()

def out(verdict, reason, detail):
    print('%s|%s|%s' % (verdict, reason, detail))

if code == '000':
    out('BLOCK', 'no-provider', 'proxy unreachable'); raise SystemExit(0)

d = None
try:
    d = json.loads(body)
except Exception:
    d = None

if isinstance(d, dict):
    err = str(d.get('error') or '')
    # Exact sentinels first. NEVER substring-match 'no_candidate_lane': it is a
    # BOOLEAN field that appears in capacity bodies as `false`, and matching the
    # name alone mislabels a transient drain as a permanent config fault
    # (own bug, caught by a live probe 2026-10-02).
    if err == 'no lane declares this model (flat router)' or d.get('no_candidate_lane') is True:
        out('BLOCK', 'no-lane', 'no lane declares model=%s (config fault: a retry can never fix it)' % model)
    elif err == 'all providers exhausted (flat router)' or d.get('capacity_exhausted') is True:
        out('BLOCK', 'capacity', 'all providers exhausted for model=%s (transient)' % model)
    elif 'egress degraded' in err.lower():
        out('BLOCK', 'no-provider', 'egress degraded for model=%s' % model)
    elif code == '200' and 'choices' in d:
        out('ALLOW', 'probe', 'model=%s served' % model)
    else:
        out('BLOCK', 'no-provider', 'http=%s model=%s error=%s' % (code, model, (err or 'none')[:80]))
else:
    # Unparseable body: fall back to the sentinel STRINGS only (never field names).
    if 'no lane declares this model' in low:
        out('BLOCK', 'no-lane', 'no lane declares model=%s' % model)
    elif 'all providers exhausted' in low:
        out('BLOCK', 'capacity', 'all providers exhausted for model=%s' % model)
    elif code == '200':
        out('BLOCK', 'no-provider', 'http=200 but unparseable body model=%s' % model)
    else:
        out('BLOCK', 'no-provider', 'http=%s model=%s unparseable' % (code, model))
" 2>/dev/null || echo "BLOCK|unknown|classifier failed")
rm -f "$PROBE_BODY_FILE"

VERDICT_KIND="${VERDICT%%|*}"
VERDICT_REST="${VERDICT#*|}"
VERDICT_REASON="${VERDICT_REST%%|*}"
VERDICT_DETAIL="${VERDICT_REST#*|}"

if [ "$VERDICT_KIND" = "ALLOW" ]; then
    echo "cheap-gate: ALLOW via fallback-probe ($VERDICT_DETAIL)" >&2
    exit 0
fi

if [ "$VERDICT_REASON" = "capacity" ]; then
    python3 -c "import json,time,os,sys;p=os.path.expanduser('~/.hermes/bot/.capacity_held.jsonl');open(p,'a').write(json.dumps({'ts':int(time.time()),'label':sys.argv[1]})+chr(10))" "$HOLD_LABEL" 2>/dev/null || true
fi
echo "cheap-gate: BLOCK $VERDICT_REASON ($VERDICT_DETAIL)" >&2
exit 1