#!/bin/bash
# Weekly follow-up gate for the client pipeline (generic; client values live in
# the private env file sourced below — never in this public repo).
# Logic: if the client had PUBLIC nostr content activity within the quiet
# window, or was nudged within the last 6 days, print SKIP context (the LLM
# must then stay SILENT). Otherwise mark a nudge in the state file and print
# context for the weekly follow-up message.
export PATH="$HOME/.local/bin:$HOME/go/bin:$HOME/.hermes/hermes-agent/venv/bin:$PATH"

CFG="$HOME/.hermes/profiles/manager/state/client-pipeline.env"
[ -f "$CFG" ] && . "$CFG"
NPUB_HEX="${CLIENT_NPUB_HEX:?missing config: $CFG}"
STATE="${CLIENT_FOLLOWUP_STATE:?missing config: $CFG}"
MIN_QUIET_DAYS="${CLIENT_QUIET_DAYS:-7}"

# --- public CONTENT activity check (neutral shared relays only; kind 0 profile
#     edits are setup noise, not activity) ---
LAST_ACT=$(nak req wss://relay.damus.io wss://nos.lol wss://relay.primal.net \
  -a "$NPUB_HEX" -k 1 -k 30023 -k 20 -k 1063 --limit 50 2>/dev/null \
  | python3 -c "
import sys, json
latest = 0
for line in sys.stdin:
    try:
        d = json.loads(line)
    except Exception:
        continue
    c = d.get('created_at') or 0
    if isinstance(c, int) and c > latest:
        latest = c
print(latest)")
LAST_ACT="${LAST_ACT:-0}"
case "$LAST_ACT" in (*[!0-9]*|"") LAST_ACT=0;; esac
NOW=$(date +%s)
QUIET_DAYS=$(( (NOW - LAST_ACT) / 86400 ))
if [ "$LAST_ACT" -gt 0 ] && [ "$QUIET_DAYS" -lt "$MIN_QUIET_DAYS" ]; then
  echo "STATUS=SKIP: client had public activity ${QUIET_DAYS}d ago (quiet window ${MIN_QUIET_DAYS}d). No nudge this week."
  exit 0
fi

# --- weekly cadence dedup ---
LAST_NUDGE=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(d.get('last_nudge_ts', 0))" "$STATE")
LAST_NUDGE="${LAST_NUDGE:-0}"
case "$LAST_NUDGE" in (*[!0-9]*|"") LAST_NUDGE=0;; esac
SINCE_NUDGE=$(( (NOW - LAST_NUDGE) / 86400 ))
if [ "$SINCE_NUDGE" -lt 6 ]; then
  echo "STATUS=SKIP: nudged ${SINCE_NUDGE}d ago (weekly cadence)."
  exit 0
fi

# --- mark this nudge + emit context for the LLM (atomic write) ---
python3 - "$STATE" "$NOW" "$QUIET_DAYS" << 'EOF'
import json, sys, os, tempfile
path, now, quiet = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    d = json.load(open(path))
except Exception:
    d = {}
d['last_nudge_ts'] = now
d['nudge_count'] = d.get('nudge_count', 0) + 1
d['quiet_days_at_last_nudge'] = quiet
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=parent or '.')
with os.fdopen(fd, 'w') as f:
    json.dump(d, f, indent=1)
os.replace(tmp, path)
EOF

LAST_TOPIC=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
t = d.get('topics') or []
print(t[-1] if t else '<none>')" "$STATE")

if [ "$LAST_ACT" -eq 0 ]; then
  echo "STATUS=NUDGE: no public content events by the client found on neutral relays."
else
  echo "STATUS=NUDGE: client quiet for ${QUIET_DAYS}d."
fi
echo "STATE_FILE=$STATE"
echo "LAST_TOPIC (vary the angle this week, do not repeat verbatim): $LAST_TOPIC"
