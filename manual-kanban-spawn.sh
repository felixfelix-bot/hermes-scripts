#!/usr/bin/env bash
# manual-kanban-spawn.sh — spawn a kanban worker WITHOUT the dispatcher.
#
# Why: `hermes kanban dispatch` silently returns spawned:[] on this box (the
# ledger gate prices every lane +inf -> dispatch_headroom llm=0). The
# kanban-crash-wrapper.sh also refuses worker runs via its own FLEET GATE.
# This replicates the dispatcher's exact Popen contract, bypassing both.
#
# Usage: manual-kanban-spawn.sh <board> <task_id> <profile> [workspace]
#
# FIX 2026-10-05 #1: workspace is READ FROM THE BOARD DB (tasks.workspace_path),
#   not guessed. Repo-project worktrees live at <repo>/.worktrees/<task>, not
#   ~/worktrees/<task>; guessing died with "no workspace".
#
# FIX 2026-10-05 #2 (CRITICAL): REGISTER THE CHILD PID. Without it
#   tasks.worker_pid stays NULL and the daemon's reconcile loop treats the
#   claim as foreign and RECLAIMS THE CARD WHILE THE WORKER IS STILL RUNNING
#   (observed: reclaimed with a 57s-old heartbeat; the worker kept working 14
#   more minutes against an orphaned card).
#
# FIX 2026-10-05 #3 (CRITICAL): DUPLICATE-SPAWN GUARD. Spawning a second worker
#   onto a card that ALREADY HAS A LIVE WORKER puts two agents in ONE worktree:
#   they race each other's edits/commits/checkouts and can corrupt or lose work.
#   This script tracked the LATEST pid, so it could not even see the first one.
#   Now it refuses while the recorded pid is alive. (Observed on t_119a4ab8.)
set -uo pipefail

BOARD="${1:?board}"; TASK="${2:?task id}"; PROFILE="${3:?profile}"
HOME_DIR="/home/c03rad0r"
HERMES_BIN="$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes"
DB="$HOME_DIR/.hermes/kanban/boards/$BOARD/kanban.db"
CLAIM_TTL="${CLAIM_TTL:-7200}"   # seconds; a long worker must not be reclaimed mid-flight

# --- resolve the workspace (authoritative source, in order) -----------------
WS="${4:-}"
if [[ -z "$WS" && -f "$DB" ]]; then
    WS="$(sqlite3 "$DB" \
        "select coalesce(workspace_path,'') from tasks where id='$TASK';" 2>/dev/null)"
fi
if [[ -z "$WS" ]]; then
    for cand in "$HOME_DIR/worktrees/$TASK" "$HOME_DIR/repos/$BOARD/.worktrees/$TASK"; do
        [[ -d "$cand" ]] && { WS="$cand"; break; }
    done
fi
[[ -n "$WS" && -d "$WS" ]] || { echo "no workspace for $TASK (db='${WS:-none}')" >&2; exit 1; }

# --- DUPLICATE-SPAWN GUARD: never put two workers in one worktree ----------
EXISTING="$(sqlite3 "$DB" "select coalesce(worker_pid,'') from tasks where id='$TASK';" 2>/dev/null)"
if [[ -n "$EXISTING" ]] && kill -0 "$EXISTING" 2>/dev/null; then
    echo "REFUSING: $BOARD/$TASK already has a LIVE worker (pid $EXISTING)." >&2
    echo "  Two workers in $WS would race each other's edits and lose work." >&2
    echo "  Wait for it, or kill $EXISTING deliberately (push unpushed commits FIRST)." >&2
    exit 2
fi
# Also catch an untracked agent already sitting in the worktree.
if pgrep -f "HERMES_KANBAN_TASK=$TASK" >/dev/null 2>&1; then
    echo "REFUSING: a process for $TASK is already running (not recorded in DB)." >&2
    pgrep -af "HERMES_KANBAN_TASK=$TASK" >&2
    exit 2
fi

# --- SESSION-LIMIT PRE-FLIGHT (FIX #6) --------------------------------------
# The child can die BEFORE its first API call (Hermes caps concurrent sessions;
# a startup error exits immediately). The old script registered worker_pid
# AFTER launching, so a dead-on-arrival child still left a phantom claim that
# looked like a live worker ("3/3 held by cli", observed on t_ac001750).
# Count the real registry BEFORE launching: refuse cleanly, register nothing.
SESS_FILE="$HOME_DIR/.hermes/profiles/$PROFILE/runtime/active_sessions.json"
SESS_MAX="$(python3 - "$HOME_DIR/.hermes/profiles/$PROFILE/config.yaml" <<'PY' 2>/dev/null || echo 3
import sys,re
try:
    t=open(sys.argv[1]).read()
    m=re.search(r'^max_concurrent_sessions:\s*(\d+)', t, re.M)
    print(m.group(1) if m else 3)
except Exception:
    print(3)
PY
)"
if [[ -f "$SESS_FILE" ]]; then
    SESS_N="$(python3 -c "
import json,sys
try:
    d=json.load(open('$SESS_FILE'))
    ent=d.get('entries', d) if isinstance(d,dict) else d
    print(len(ent) if ent else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)"
    if [[ "${SESS_N:-0}" -ge "${SESS_MAX:-3}" ]]; then
        echo "REFUSING: at the Hermes session limit (${SESS_N}/${SESS_MAX}) — nothing registered." >&2
        echo "  Slots are held by live sessions in $SESS_FILE — let one finish." >&2
        exit 3
    fi
fi

# Strip any inherited gateway-turn session routing (the dispatcher does the same).
for k in $(env | grep -oE '^HERMES_SESSION_[A-Z_]*'); do unset "$k"; done
unset HERMES_CRON_AUTO_DELIVER_TO HERMES_UI_SESSION_ID AI_AGENT HERMES_AGENT 2>/dev/null || true

echo "[manual-spawn] board=$BOARD task=$TASK profile=$PROFILE ws=$WS" >&2

# --- launch child in the BACKGROUND so we can register its PID --------------
# FIX 2026-10-05 #5: MODEL_OVERRIDE. Cross-family review (D-115/D-128) needs a
#   reviewer from a DIFFERENT family than the author. The profile default
#   (tier/coding-worker -> GLM) would self-review. MODEL_OVERRIDE=tier/review-kimi
#   pins the reviewer lane; unset means the profile default.
MODEL_ARGS=()
[[ -n "${MODEL_OVERRIDE:-}" ]] && MODEL_ARGS=(-m "$MODEL_OVERRIDE")

env \
  HERMES_HOME="$HOME_DIR/.hermes/profiles/$PROFILE" \
  HERMES_KANBAN_TASK="$TASK" \
  HERMES_KANBAN_BOARD="$BOARD" \
  HERMES_KANBAN_WORKSPACE="$WS" \
  HERMES_SESSION_SOURCE="kanban" \
  TERMINAL_CWD="$WS" \
  "$HERMES_BIN" -p "$PROFILE" chat "${MODEL_ARGS[@]}" -q "work kanban task $TASK" &
CHILD=$!

# FIX #6b: verify the child survived startup BEFORE registering it. A child that
# dies on its first API call (session cap, bad model, 503) must leave NO claim.
sleep 3
if ! kill -0 "$CHILD" 2>/dev/null; then
    echo "REFUSING: child died during startup (pid $CHILD) — nothing registered." >&2
    exit 4
fi

# Register the PID — without this the daemon reclaims a live worker's card.
# FIX #4: also set status='running'. Registering only worker_pid left the row
# at 'ready', so the card looked idle while a worker held it and the dispatcher
# could hand it out again.
sqlite3 "$DB" "update tasks set status='running', worker_pid=$CHILD,
    claim_lock='$(hostname):$CHILD',
    claim_expires=strftime('%s','now')+$CLAIM_TTL,
    last_heartbeat_at=strftime('%s','now')
  where id='$TASK';" 2>/dev/null || true
echo "[manual-spawn] registered worker_pid=$CHILD + status=running on $BOARD/$TASK" >&2

wait "$CHILD"
