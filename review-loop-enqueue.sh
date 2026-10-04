#!/bin/bash
# review-loop-enqueue.sh — cron entry: turn an unanswered external review round on
# one of OUR PRs into a fix card, for every enabled review target. No-agent.
set -uo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python3"; [ -x "$PY" ] || PY=python3
exec "$PY" "$HOME/.hermes/scripts/review_loop.py" enqueue "$@"
