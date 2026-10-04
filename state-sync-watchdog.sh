#!/usr/bin/env bash
# state-sync-watchdog.sh — thin shim so the Hermes no_agent cron job can run
# the canonical state_sync_watchdog.py that lives inside the hermes-orchestration
# checkout (same pattern as state-sync-cron.sh).
#
# Silent on success (empty stdout); on a trip it emits an alert block that the
# cron layer delivers verbatim. Exit code is always 0 — the message IS the signal.
set -euo pipefail

REPO="${HERMES_ORCHESTRATION_REPO:-$HOME/hermes-orchestration}"
SCRIPT="$REPO/scripts/sync/state_sync_watchdog.py"

if [[ ! -f "$SCRIPT" ]]; then
    echo "state-sync-watchdog: ERROR — $SCRIPT not found." >&2
    exit 2
fi

exec python3 "$SCRIPT" "$@"
