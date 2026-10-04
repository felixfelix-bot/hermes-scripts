#!/usr/bin/env bash
# hermes_tmp_hygiene.sh — keep /tmp (a small RAM tmpfs) and the fleet TMPDIR
# from filling with stale agent/task scratch.
#
# - removes our own /tmp entries older than 6h (never touches other users, X*)
# - removes our TMPDIR entries older than 24h
# Safe: only entries owned by the invoking user, top level only.
set -u
me="$(id -un)"
target="${TMPDIR:-$HOME/.cache/hermes/tmp}"

find /tmp -mindepth 1 -maxdepth 1 -user "$me" -mmin +360 -not -name '.X*' \
     -exec rm -rf {} + 2>/dev/null || true

if [ "$target" != "/tmp" ] && [ -d "$target" ]; then
  find "$target" -mindepth 1 -maxdepth 1 -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
fi
