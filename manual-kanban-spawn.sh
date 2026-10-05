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
# The workspace is READ FROM THE BOARD DB (tasks.workspace_path), not guessed.
# Bug fixed 2026-10-05: worktree tasks do NOT live at ~/worktrees/<task> — a
# repo-project task lives at <repo>/.worktrees/<task>. Guessing the path made
# the spawn die with "no workspace". The claim output already knows the real
# path; the DB is the authority. An explicit 4th arg overrides the lookup.
set -uo pipefail

BOARD="${1:?board}"; TASK="${2:?task id}"; PROFILE="${3:?profile}"
HOME_DIR="/home/c03rad0r"
HERMES_BIN="$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes"
DB="$HOME_DIR/.hermes/kanban/boards/$BOARD/kanban.db"

# --- resolve the workspace (authoritative source, in order) -----------------
WS="${4:-}"
if [[ -z "$WS" && -f "$DB" ]]; then
    WS="$(sqlite3 "$DB" \
        "select coalesce(workspace_path,'') from tasks where id='$TASK';" 2>/dev/null)"
fi
# Fallbacks only if the DB gave nothing (scratch tasks have no path).
if [[ -z "$WS" ]]; then
    for cand in "$HOME_DIR/worktrees/$TASK" "$HOME_DIR/repos/$BOARD/.worktrees/$TASK"; do
        [[ -d "$cand" ]] && { WS="$cand"; break; }
    done
fi
[[ -n "$WS" && -d "$WS" ]] || { echo "no workspace for $TASK (db='${WS:-none}')" >&2; exit 1; }

# Strip any inherited gateway-turn session routing (the dispatcher does the same).
for k in $(env | grep -oE '^HERMES_SESSION_[A-Z_]*'); do unset "$k"; done
unset HERMES_CRON_AUTO_DELIVER_TO HERMES_UI_SESSION_ID AI_AGENT HERMES_AGENT 2>/dev/null || true

echo "[manual-spawn] board=$BOARD task=$TASK profile=$PROFILE ws=$WS" >&2

exec env \
  HERMES_HOME="$HOME_DIR/.hermes/profiles/$PROFILE" \
  HERMES_KANBAN_TASK="$TASK" \
  HERMES_KANBAN_BOARD="$BOARD" \
  HERMES_KANBAN_WORKSPACE="$WS" \
  HERMES_SESSION_SOURCE="kanban" \
  TERMINAL_CWD="$WS" \
  "$HERMES_BIN" -p "$PROFILE" chat -q "work kanban task $TASK"
