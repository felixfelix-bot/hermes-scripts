#!/usr/bin/env bash
# merge-queue-digest.sh — resource-limited wrapper for merge_queue_digest.py
# Read-only merge-queue digest (supersedes branch-staleness-scanner, cron 2c710ccb415c).
# State/heartbeat/lock live under ~/.hermes/profiles/manager/cron/output/.
set -u
OUT_DIR="$HOME/.hermes/profiles/manager/cron/output"
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"
exec nice -n 19 ionice -c3 timeout 900 python3 \
  "$HOME/.hermes/profiles/manager/scripts/merge_queue_digest.py" "$@"
