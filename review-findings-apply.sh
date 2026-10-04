#!/bin/bash
# review-findings-apply.sh — cron entry: drain ~/.hermes/state/review_findings/*.json
# through review_fix_emit.py (deterministic findings -> fix cards; decisions ->
# operator queue). No-agent, 0 tokens.
set -uo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python3"; [ -x "$PY" ] || PY=python3
exec "$PY" "$HOME/.hermes/scripts/review_findings_apply.py" "$@"
