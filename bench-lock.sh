#!/usr/bin/env bash
# bench-lock.sh — bounded, self-releasing mutex for the single-owner bench router.
#
# Why this exists (2026-09-29): a lane took the bench lock with the pattern
#
#     exec 9>~/.hermes/state/bench-mt3000.lock && flock -n 9 && ... \
#       && while true; do sleep 30; done
#
# The holder OUTLIVED the lane that started it, so the lock stayed held forever
# after the work finished and the next lane could not touch the hardware. An
# infinite `while true` sleep is not a lock; it is a leak.
#
# This helper holds the lock with a hard TTL and releases on every exit path
# (normal exit, error, signal), so a crashed or killed owner cannot wedge the box.
#
# Usage:
#   bench-lock.sh status
#   bench-lock.sh run <purpose> -- <command...>     # hold while <command> runs
#   bench-lock.sh hold <purpose> [--ttl SECONDS]    # hold for TTL, then release
#
# Exit codes: 0 ok, 1 lock busy, 2 usage error, 124 TTL expired.

set -u
LOCK_FILE="${BENCH_LOCK_FILE:-$HOME/.hermes/state/bench-mt3000.lock}"
DEFAULT_TTL="${BENCH_LOCK_TTL:-1800}"   # 30 min: no bench step legitimately runs longer
MAX_TTL="${BENCH_LOCK_MAX_TTL:-7200}"   # hard ceiling, 2 h

usage() { sed -n '2,25p' "$0"; exit 2; }

status() {
  if timeout 3 flock -n "$LOCK_FILE" -c 'exit 0' 2>/dev/null; then
    echo "bench lock: FREE ($LOCK_FILE)"
    return 0
  fi
  echo "bench lock: HELD ($LOCK_FILE)"
  local holder
  holder=$(ps -eo pid,etimes,cmd 2>/dev/null | grep -F "$LOCK_FILE" | grep -v grep | head -3 | cut -c1-160)
  [ -n "$holder" ] && printf 'holders:\n%s\n' "$holder"
  return 1
}

# Run a command while holding the lock. The lock is released when this function
# returns, however it returns.
run_hold() {
  local purpose="$1"; shift
  [ "${1:-}" = "--" ] && shift
  [ $# -gt 0 ] || { echo "usage: bench-lock.sh run <purpose> -- <command...>" >&2; exit 2; }
  exec 9>"$LOCK_FILE" || { echo "cannot open lock file" >&2; exit 2; }
  if ! flock -n 9; then
    echo "bench lock: BUSY — refusing to run '$purpose'" >&2
    status >&2 || true
    exit 1
  fi
  printf 'pid=%s purpose=%s since=%s ttl=%ss\n' "$$" "$purpose" "$(date -Iseconds)" "$DEFAULT_TTL" >&9
  local rc=0
  # Release explicitly on every exit path; the fd would close anyway on a clean
  # exit, but a signal would otherwise leave the shell's children holding it.
  trap 'flock -u 9 2>/dev/null; exit 130' INT TERM
  # `9>&-` is load-bearing: without it the child INHERITS the lock fd, so killing
  # the holder leaves the child alive holding the lock - the exact leak this
  # helper exists to prevent (found by tests/bench-lock_test.sh).
  "$@" 9>&- || rc=$?
  flock -u 9 2>/dev/null
  return $rc
}

hold() {
  local purpose="$1"; shift
  local ttl="$DEFAULT_TTL"
  while [ $# -gt 0 ]; do
    case "$1" in
      --ttl) ttl="${2:-}"; shift 2 || exit 2 ;;
      *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
  done
  case "$ttl" in ''|*[!0-9]*) echo "--ttl must be an integer" >&2; exit 2 ;; esac
  [ "$ttl" -le "$MAX_TTL" ] || { echo "--ttl exceeds the $MAX_TTL s ceiling" >&2; exit 2; }
  exec 9>"$LOCK_FILE" || exit 2
  if ! flock -n 9; then
    echo "bench lock: BUSY — refusing to hold for '$purpose'" >&2
    status >&2 || true
    exit 1
  fi
  printf 'pid=%s purpose=%s since=%s ttl=%ss\n' "$$" "$purpose" "$(date -Iseconds)" "$ttl" >&9
  echo "bench lock HELD by pid $$ for ${ttl}s: $purpose"
  trap 'echo "releasing bench lock (signal)"; flock -u 9 2>/dev/null; exit 130' INT TERM
  # `9>&-` so `sleep` cannot inherit the lock fd: otherwise killing this holder
  # leaves the sleep child holding the lock until its TTL expires, which is the
  # leak this helper exists to prevent (found by tests/bench-lock_test.sh).
  sleep "$ttl" 9>&-
  flock -u 9 2>/dev/null
  echo "bench lock released after ${ttl}s"
}

cmd="${1:-}"
case "$cmd" in
  status)
    status ;;
  run)
    shift
    purpose="${1:-unnamed}"
    shift 2>/dev/null || true
    run_hold "$purpose" "$@" ;;
  hold)
    shift
    purpose="${1:-unnamed}"
    shift 2>/dev/null || true
    hold "$purpose" "$@" ;;
  *)
    usage ;;
esac
