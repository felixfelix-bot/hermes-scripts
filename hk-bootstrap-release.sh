#!/bin/bash
# hk-bootstrap-release.sh — BOOTSTRAP release for the house-keeping board.
# Chicken-and-egg fix: HK-3 (t_516327d5) builds the permanent watchdog
# (hk-release.sh), but HK-3 itself sits in 'scheduled' behind that watchdog.
# This manager-direct shim releases HK-1 ONE time when quota gates pass.
# HK-3's own watchdog supersedes this once built; this shim then no-ops.
#
# Quota policy (audit-2 deltas): null telemetry + proxy ALIVE -> allow
# (telemetry outage must not block dispatch); proxy DEAD -> hold.
# Pressure gates: weekly <60 AND 5h <40 on both keys.
# Empty stdout = silent. Non-empty = delivered to operator (deliver=origin).

set -u
BOARD=house-keeping
HERMES=$(command -v hermes || echo /home/c03rad0r/.local/bin/hermes)
STATE="$HOME/.hermes/bot/hk_bootstrap_state.json"

dbg() { [ "${VERBOSE:-0}" = "1" ] && echo "[dbg] $*" >&2 || true; }

# --- Proxy health (fail-closed on dead proxy) ---
PROXY_ALIVE=$(curl -sf --max-time 5 -o /dev/null -w '%{http_code}' http://localhost:9099/health 2>/dev/null) || PROXY_ALIVE="000"
if [ "$PROXY_ALIVE" != "200" ]; then
    dbg "proxy dead ($PROXY_ALIVE): hold"
    exit 0
fi

# --- Quota gate decision (all logic in python; prints ALLOW or HOLD) ---
DECISION=$(python3 - "$HOME" <<'PYEOF'
import json, sys, urllib.request

home = sys.argv[1]

def fetch_json(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None

def window_pct(d, key, wname):
    try:
        for w in d.get(key, {}).get('windows', []):
            if wname in w.get('name', ''):
                return w.get('used_pct')
    except Exception:
        pass
    return None

quota = fetch_json('http://localhost:9099/quota')

if quota is None:
    # Null telemetry + proxy alive -> allow (audit-2 D3 scoping)
    print('ALLOW telemetry-outage-proxy-alive')
    sys.exit(0)

ours_w   = window_pct(quota, 'ours', 'weekly')
ours_5h  = window_pct(quota, 'ours', '5-hour')
fr_w     = window_pct(quota, 'friend', 'weekly')
fr_5h    = window_pct(quota, 'friend', '5-hour')

# zai_state friend_token_pct = REMAINING pct on friend key (state file)
friend_state_used = None
try:
    st = json.load(open(f'{home}/.hermes/bot/zai_state.json'))
    rem = st.get('friend_token_pct')
    if rem is not None:
        friend_state_used = max(0, 100 - int(rem))
except Exception:
    pass

# No live quota data at all -> telemetry outage, proxy alive -> allow
if all(v is None for v in (ours_w, ours_5h, fr_w, fr_5h)) and friend_state_used is None:
    print('ALLOW telemetry-outage-proxy-alive')
    sys.exit(0)

# Binary lock: any 5h window at >=100 = locked
for v in (ours_5h, fr_5h):
    if v is not None and v >= 100:
        print('HOLD binary-5h-lock')
        sys.exit(0)

# Pressure: weekly <60 AND 5h <40 on every key with data
checks = []
if ours_w  is not None: checks.append(ours_w  < 60)
if ours_5h is not None: checks.append(ours_5h < 40)
if fr_w    is not None: checks.append(fr_w    < 60)
if fr_5h   is not None: checks.append(fr_5h   < 40)
if friend_state_used is not None:
    # friend weekly proxy: state-file used pct must also be <60
    checks.append(friend_state_used < 60)

reason = f"ours_w={ours_w} ours_5h={ours_5h} fr_w={fr_w} fr_5h={fr_5h} fr_state_used={friend_state_used}"
if checks and all(checks):
    print('ALLOW ' + reason)
else:
    print('HOLD ' + reason)
PYEOF
) || DECISION="HOLD gate-error"

dbg "decision: $DECISION"

case "$DECISION" in
  ALLOW*) ;;
  *) exit 0 ;;   # HOLD or gate-error -> silent
esac

# --- Gates pass. Release HK-1 once. ---
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

if [ "$(st hk1_released)" = "1" ]; then
    dbg "already released: silent"
    exit 0
fi

HK1_STATUS=$("$HERMES" kanban --board "$BOARD" list --json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
tasks = d if isinstance(d, list) else d.get('tasks', [])
for t in tasks:
    if t.get('id') == 't_b614c748':
        print(t.get('status', '')); break
" 2>/dev/null) || HK1_STATUS="error"

dbg "HK-1 status: $HK1_STATUS"

case "$HK1_STATUS" in
  scheduled|blocked)
    if "$HERMES" kanban --board "$BOARD" unblock t_b614c748 --reason 'bootstrap: quota gates passed (weekly<60/5h<40 both keys) — releasing HK-1 creator helper; HK-3 permanent watchdog supersedes this shim' >/dev/null 2>&1; then
        setst hk1_released 1
        echo "HOUSEKEEPING BOOTSTRAP: quota cheap ($DECISION). Released HK-1 creator helper (t_b614c748) on board house-keeping. Chain: HK-1 done → HK-2+HK-3 release as parents complete → HK-4 pilots → HK-5 docs. HK-3 builds the permanent release watchdog which replaces this shim."
    else
        dbg "unblock failed: silent (retry next tick)"
    fi
    ;;
  done|archived|ready|running|review|in_progress|error)
    # Chain already moving or HK-1 completed — mark released, stay silent.
    setst hk1_released 1
    dbg "HK-1 state $HK1_STATUS: mark released, silent"
    ;;
  *)
    dbg "HK-1 unknown state '$HK1_STATUS': silent"
    ;;
esac
exit 0