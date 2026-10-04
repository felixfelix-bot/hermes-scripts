#!/usr/bin/env bash
# bulk-serial.sh — serialize heavy bulk jobs (gh / ansible / kanban / db scans)
# behind ONE advisory lock and run them at low priority, so they never overlap
# each other or a worker build (2026-09-30 incident: 8 concurrent ansible +
# db-health scans + git sync drove load to 50 on 4 cores).
#
# Usage:  bulk-serial.sh <command> [args...]
#
# -w 300: wait up to 5 min for the lock; if a heavy job still holds it, this run
#          is skipped (never queued unboundedly). nice 19 + ionice idle: bulk
#          jobs yield to the gateway and workers.
set -u
uid="$(id -u)"
runtime="${XDG_RUNTIME_DIR:-/run/user/$uid}"
mkdir -p "$runtime" 2>/dev/null || true
lock="$runtime/hermes-bulk.lock"
exec /usr/bin/flock -w "${BULK_LOCK_WAIT:-300}" "$lock" \
    /usr/bin/nice -n 19 \
    /usr/bin/ionice -c3 "$@"
