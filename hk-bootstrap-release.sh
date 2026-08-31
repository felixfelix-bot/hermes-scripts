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

# --- Gates pass. Status-driven chain release. Board state = truth. ---
# Order follows task_links: HK-1 head; HK-2/HK-3/HK-6 after HK-1;
# HK-4 after HK-2 AND HK-3; HK-5 after HK-4. Idempotent — a task in
# ready/running/done is skipped; CLI-transient failures retry next tick.
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

# Superseded by HK-3's permanent watchdog — silent forever.
if [ "$(st superseded)" = "1" ]; then
    dbg "superseded: silent"
    exit 0
fi

task_status() {
    "$HERMES" kanban --board "$BOARD" list --json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
tasks = d if isinstance(d, list) else d.get('tasks', [])
for t in tasks:
    if t.get('id') == '$1':
        print(t.get('status', '')); break
" 2>/dev/null
}

HK1=t_b614c748; HK2=t_bb3d7343; HK3=t_516327d5; HK4=t_2b4369c3; HK5=t_cea9ca72; HK6=t_b58a9b63

is_done() { case "$1" in done|completed|archived) return 0;; *) return 1;; esac; }

MSG=""
release_if_scheduled() {  # <id> <phase-note>
    local ts; ts=$(task_status "$1")
    case "$ts" in
      scheduled|blocked)
        if "$HERMES" kanban --board "$BOARD" unblock "$1" --reason "bootstrap $2: quota gates passed ($DECISION)" >/dev/null 2>&1; then
            MSG="$MSG $1"
            return 0
        fi
        ;;
    esac
    return 1
}

S1=$(task_status "$HK1")
if is_done "$S1"; then
    # PHASE 2: head done — release its direct dependents + independent HK-6.
    release_if_scheduled "$HK2" 'phase 2' || true
    release_if_scheduled "$HK3" 'phase 2' || true
    release_if_scheduled "$HK6" 'phase 2' || true
    # HK-4 requires BOTH HK-2 and HK-3 done (task_links).
    if is_done "$(task_status "$HK2")" && is_done "$(task_status "$HK3")"; then
        release_if_scheduled "$HK4" 'phase 2b' || true
    fi
    # HK-5 requires HK-4 done.
    if is_done "$(task_status "$HK4")"; then
        release_if_scheduled "$HK5" 'phase 2c' || true
    fi
else
    # PHASE 1: chain head only.
    release_if_scheduled "$HK1" 'phase 1' || dbg "HK-1 not releasable yet (state: $S1)"
fi

# PHASE 3: HK-3 done — its permanent watchdog exists. Self-remove.
if is_done "$(task_status "$HK3")"; then
    setst superseded 1
    SELF_ID=$(python3 -c "
import json
for path in ('$HOME/.hermes/profiles/manager/cron/jobs.json', '$HOME/.hermes/cron/jobs.json'):
    try:
        d = json.load(open(path))
    except Exception:
        continue
    jobs = d.get('jobs', d) if isinstance(d, dict) else d
    for j in jobs:
        if j.get('name') == 'hk-bootstrap-release':
            print(j.get('id', '')); raise SystemExit(0)
" 2>/dev/null)
    if [ -n "${SELF_ID:-}" ]; then
        "$HERMES" cron remove "$SELF_ID" >/dev/null 2>&1 && dbg "cron self-removed ($SELF_ID)"
    fi
    MSG="${MSG:+$MSG }[shim superseded by HK-3 watchdog, cron self-removed]"
fi

if [ -n "$MSG" ]; then
    echo "HOUSEKEEPING BOOTSTRAP: quota cheap ($DECISION). Released:$MSG. Board house-keeping chain self-driving from here."
fi
exit 0