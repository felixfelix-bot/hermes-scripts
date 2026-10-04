#!/bin/bash
# worktree-quarantine.sh — weekly orphan-worktree quarantine to DQ05 (1-year).
set -uo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
exec nice -n 19 ionice -c3 "$PY" "$HOME/.hermes/scripts/worktree_quarantine.py" \
  --policy "$HOME/.hermes/bot/worktree_quarantine.json" --apply
