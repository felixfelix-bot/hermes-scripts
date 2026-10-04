#!/usr/bin/env bash
# lkg-snapshot.sh — frequent last-known-good snapshot of live router + config.
# Installed to ~/.hermes/scripts/ and run on a short cron (D-133).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LKG="$HERE/fleet_lkg.py"
[ -f "$LKG" ] || LKG="$HOME/.hermes/scripts/fleet_lkg.py"
if [ -f "$LKG" ]; then
  python3 "$LKG" snapshot --label "$(date -u +%Y%m%dT%H%M%SZ)" >/dev/null 2>&1 || true
  python3 "$LKG" prune --keep "${LKG_KEEP:-96}" >/dev/null 2>&1 || true
fi
# Also refresh the version-controlled Kalman/state export.
EXP="$HERE/router_state_export.py"
[ -f "$EXP" ] && python3 "$EXP" >/dev/null 2>&1 || true
exit 0
