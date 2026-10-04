#!/bin/bash
# telemetry-retention.sh — bounded retention for the bot telemetry DBs.
# Wrapper so the hermes no_agent cron can run it with args.
set -uo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
exec nice -n 19 ionice -c3 "$PY" "$HOME/.hermes/scripts/telemetry_retention.py" \
  --config "$HOME/.hermes/bot/telemetry_retention.json" --apply
