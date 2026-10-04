#!/bin/bash
# triage-block-guard.sh — cron wrapper (C1/C2).
# Runs the triage dead-end guard and auto-specifies worker-assigned roots
# (bounded), escalating non-worker roots to the operator. Safe/idempotent:
# the guard caps at 10/run and never re-specifies the same card.
exec python3 "$HOME/.hermes/scripts/triage_block_guard.py" \
  --apply --min-age-h "${TRIAGE_GUARD_MIN_AGE_H:-6}" \
  --max-specify "${TRIAGE_GUARD_SPECIFY_MAX:-10}"
