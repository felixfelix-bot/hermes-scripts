#!/usr/bin/env bash
# reviewer-sla-watchdog.sh — resource-limited wrapper for reviewer_sla_watchdog.py
# Reviewer-pool 4h SLA watchdog (D-114 §6, task t_55d56efa / MD-2).
# No-agent script cron every 30min; silent watchdog contract (empty stdout = quiet).
set -u
OUT_DIR="$HOME/.hermes/profiles/manager/cron/output"
BOARD_DB="$HOME/.hermes/kanban/boards/merge-deputy/kanban.db"
mkdir -p "$OUT_DIR"
exec nice -n 19 ionice -c3 timeout 120 python3 \
  "$HOME/.hermes/scripts/reviewer_sla_watchdog.py" \
  --db "$BOARD_DB" \
  --state-file "$OUT_DIR/reviewer_sla_state.json" \
  --sla-hours 4 \
  --politeness-hours 6 \
  --max-reassign 1 \
  --halt-file "$HOME/.hermes/scripts/REVIEWER_HALT"
