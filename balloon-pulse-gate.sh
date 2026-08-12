#!/bin/bash
# balloon-pulse-gate.sh — Activity-gated status pulse for balloon sub-managers
#
# Checks for git activity in balloon track worktrees since last pulse.
# SILENT (no output, exit 0) when nothing changed → no Signal message, zero tokens.
# Outputs a brief activity summary when commits ARE found → delivered to group.
#
# Watchdog pattern: no_agent=true, deliver=origin, silent on idle.
# State file tracks last-seen commit hash per worktree.
#
# Usage: balloon-pulse-gate.sh [track-name]
# If track-name omitted, checks ALL balloon worktrees.

set -euo pipefail

STATE_FILE="$HOME/.hermes/profiles/manager/state/balloon-pulse-state.json"
WORKTREE_BASE="$HOME/worktrees"

# All known balloon track worktrees
TRACKS=(
    balloon-hermes
    balloon-fips
    balloon-range-tests
    balloon-speed-tests
    balloon-circuit-design
    balloon-tollgate
    balloon-pow
    balloon-nostr
    balloon-blossom
    balloon-pre-stretching
)

# Filter to single track if specified
if [ -n "${1:-}" ]; then
    TRACKS=("$1")
fi

# Init state file
if [ ! -f "$STATE_FILE" ]; then
    echo '{}' > "$STATE_FILE"
fi

# --- Check each worktree for new commits ---
HAS_ACTIVITY=0
SUMMARY=""

for track in "${TRACKS[@]}"; do
    worktree="$WORKTREE_BASE/$track"

    if [ ! -d "$worktree/.git" ] && [ ! -f "$worktree/.git" ]; then
        continue
    fi

    # Get current HEAD commit hash
    current_hash=$(cd "$worktree" && git rev-parse HEAD 2>/dev/null || echo "")
    if [ -z "$current_hash" ]; then
        continue
    fi

    # Get last seen hash from state
    last_hash=$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('$track', {}).get('last_hash', ''))
except: print('')
" 2>/dev/null || echo "")

    # Get last pulse timestamp
    last_ts=$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('$track', {}).get('last_pulse', 'never'))
except: print('never')
" 2>/dev/null || echo "never")

    # Count new commits since last pulse
    if [ -n "$last_hash" ] && [ "$last_hash" != "$current_hash" ]; then
        new_commits=$(cd "$worktree" && git rev-list --count "${last_hash}..HEAD" 2>/dev/null || echo 0)
    elif [ -z "$last_hash" ]; then
        # First run — just record state, don't report
        new_commits=0
    else
        new_commits=0
    fi

    if [ "$new_commits" -gt 0 ]; then
        HAS_ACTIVITY=1
        # Get short log of new commits
        new_log=$(cd "$worktree" && git log --oneline -5 "${last_hash}..HEAD" 2>/dev/null | head -5)
        SUMMARY="${SUMMARY}### ${track}
${new_commits} new commits since ${last_ts}
\`\`\`
${new_log}
\`\`\`

"
    fi

    # Update state
    python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
except: d = {}
d['$track'] = {'last_hash': '$current_hash', 'last_pulse': '$(date -Iseconds)'}
json.dump(d, open('$STATE_FILE', 'w'), indent=2)
" 2>/dev/null || true
done

# --- Output ---
if [ "$HAS_ACTIVITY" -eq 1 ]; then
    echo "BALLOON PULSE — activity detected since last check"
    echo ""
    echo "$SUMMARY"
    echo "---"
    echo "Tracks with no new commits are silent. This message only fires when work happened."
    # Exit 0 with output → delivered to group
    exit 0
else
    # No output → SILENT → no Signal message → zero tokens
    exit 0
fi