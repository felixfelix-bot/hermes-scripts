#!/usr/bin/env bash
# bulk-serial.sh — serialize heavy bulk jobs (gh / ansible / kanban / db scans)
# behind ONE advisory lock and run them at low priority, so they never overlap
# each other or a worker build (2026-09-30 incident: 8 concurrent ansible +
# db-health scans + git sync drove load to 50 on 4 cores).
#
# Usage:  bulk-serial.sh <command> [args...]
#
#
# !! INVARIANT (2026-10-06 incident) — NEVER wrap a long-running/daemon command. !!
# Wrapping tollgate-watchdog.service (Type=simple, Restart=always, endless loop) in
# this wrapper made it hold /run/user/1001/hermes-bulk.lock FOREVER: for 26h every
# other bulk job waited its 5 min and was skipped (burn collector, exhaustion gate,
# circuit breaker, board export). Before adding a drop-in, check the unit:
#     systemctl --user show -p Type --value <unit>       # oneshot = OK, simple = NO
# And check the lock is not held longer than a job should run:
#     lslocks | grep hermes-bulk ; fuser -v /run/user/$UID/hermes-bulk.lock
#
# -w ${BULK_LOCK_WAIT:-600}: wait up to 10 min for the lock; if a heavy job
#          still holds it, this run is skipped (never queued unboundedly).
#          nice 19 + ionice idle: bulk jobs yield to the gateway and workers.
#
# BULK_LOCK_NAME: the lock file name (default hermes-bulk.lock). A READ-ONLY job
#          that runs long (e.g. the full security scan) should set its own lock
#          (BULK_LOCK_NAME=hermes-bulk-read.lock) so it never starves the
#          mutating bulk jobs on the shared lock (2026-10-07: the 03:00 scan held
#          the shared lock ~42 min and every 5-min bulk job was skipped).
set -u
uid="$(id -u)"
runtime="${XDG_RUNTIME_DIR:-/run/user/$uid}"
mkdir -p "$runtime" 2>/dev/null || true
lock="$runtime/${BULK_LOCK_NAME:-hermes-bulk.lock}"
exec /usr/bin/flock -w "${BULK_LOCK_WAIT:-600}" "$lock" \
    /usr/bin/nice -n 19 \
    /usr/bin/ionice -c3 "$@"
