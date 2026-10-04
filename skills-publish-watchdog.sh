#!/usr/bin/env bash
set -euo pipefail
REPO="${HERMES_ORCHESTRATION_REPO:-$HOME/hermes-orchestration}"
SCRIPT="$REPO/scripts/operator/skills-publish-watchdog.sh"
[ -f "$SCRIPT" ] || { echo "skills-publish-watchdog: ERROR — $SCRIPT not found." >&2; exit 2; }
exec bash "$SCRIPT" "$@"
