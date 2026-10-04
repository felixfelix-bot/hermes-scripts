#!/usr/bin/env bash
# secret_sweep.sh — cron wrapper: full sweep (worktrees + history + home) + notify.
# D-128 Phase 6. Low-priority nightly via Hermes cron.
exec "$(dirname "$0")/scan_all_repos_for_secrets.sh" --notify --history "$@"
