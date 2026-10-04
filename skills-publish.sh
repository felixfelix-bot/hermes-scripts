#!/usr/bin/env bash
# skills-publish.sh — shim so the Hermes cron/timer layer can run the canonical
# skills autosave that lives inside the hermes-orchestration checkout
# (same pattern as state-sync.sh).
set -euo pipefail
REPO="${HERMES_ORCHESTRATION_REPO:-$HOME/hermes-orchestration}"
SCRIPT="$REPO/scripts/operator/skills-publish.sh"
if [ ! -f "$SCRIPT" ]; then
    echo "skills-publish: ERROR — $SCRIPT not found." >&2
    exit 2
fi
exec bash "$SCRIPT" "$@"
