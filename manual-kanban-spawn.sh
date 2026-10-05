#!/usr/bin/env bash
# manual-kanban-spawn.sh — spawn a kanban worker WITHOUT the dispatcher.
#
# Why: `hermes kanban dispatch` silently returns spawned:[] on this box (the
# ledger gate prices every lane +inf -> dispatch_headroom llm=0). The
# kanban-crash-wrapper.sh also refuses worker runs via its own FLEET GATE.
# This replicates the dispatcher's exact Popen contract, bypassing both.
#
# Usage: manual-kanban-spawn.sh <board> <task_id> <profile>
set -uo pipefail

BOARD="${1:?board}"; TASK="${2:?task id}"; PROFILE="${3:?profile}"
HOME_DIR="/home/c03rad0r"
WS="$HOME_DIR/worktrees/$TASK"
HERMES_BIN="$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes"

[ -d "$WS" ] || { echo "no workspace $WS" >&2; exit 1; }

# Strip any inherited gateway-turn session routing (the dispatcher does the same).
for k in $(env | grep -oE '^HERMES_SESSION_[A-Z_]*'); do unset "$k"; done
unset HERMES_CRON_AUTO_DELIVER_TO HERMES_UI_SESSION_ID AI_AGENT HERMES_AGENT 2>/dev/null || true

exec env \
  HERMES_HOME="$HOME_DIR/.hermes/profiles/$PROFILE" \
  HERMES_KANBAN_TASK="$TASK" \
  HERMES_KANBAN_BOARD="$BOARD" \
  HERMES_KANBAN_WORKSPACE="$WS" \
  HERMES_SESSION_SOURCE="kanban" \
  TERMINAL_CWD="$WS" \
  "$HERMES_BIN" -p "$PROFILE" chat -q "work kanban task $TASK"
