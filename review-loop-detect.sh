#!/bin/bash
# review-loop-detect.sh — cron entry: create reviewer cards for every enabled
# review target (config: ~/.hermes/bot/review_targets.json). No-agent, 0 tokens.
set -uo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python3"; [ -x "$PY" ] || PY=python3
exec "$PY" "$HOME/.hermes/scripts/review_loop.py" detect "$@"
