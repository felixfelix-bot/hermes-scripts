#!/usr/bin/env bash
# circuit-breaker.sh — thin wrapper for the D3 crash circuit-breaker.
#
# Contract (mirrors rate_limit_gate_check.sh fail-open semantics):
#   circuit-breaker.sh check <board> [task_id]
#     exit 0  -> dispatch may proceed (also: breaker healthy, log-only mode,
#                unknown board, corrupt DB — NEVER wedge dispatch on our own bugs)
#     exit 1  -> HOLD: board frozen or task tripped (enforce mode only)
#     exit 2+ -> internal error -> treat as ALLOW
#   circuit-breaker.sh scan            # breaker-daemon pass (cron every 5 min)
#   circuit-breaker.sh reset <board> <task_id> [--note ...]   # operator-only
#   circuit-breaker.sh unfreeze <board> [--note ...]
#   circuit-breaker.sh status
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${CIRCUIT_BREAKER_PY:-/usr/bin/python3}"
CORE="$HERE/circuit_breaker.py"

if [ ! -f "$CORE" ]; then
    echo "circuit-breaker core missing ($CORE) — fail-open"
    exit 0
fi

cmd="${1:-status}"
shift || true

case "$cmd" in
    check)
        board="${1:?usage: circuit-breaker.sh check <board> [task_id]}"
        task="${2:-}"
        out=$("$PY" "$CORE" check "$board" $task 2>/dev/null)
        rc=$?
        if [ "$rc" -eq 1 ]; then
            echo "$out"
            exit 1
        fi
        # rc 0 (allow) or 2+ (error) -> allow, fail-open
        [ -n "$out" ] && echo "$out"
        exit 0
        ;;
    scan|reset|unfreeze|status)
        exec "$PY" "$CORE" "$cmd" "$@"
        ;;
    *)
        echo "unknown subcommand: $cmd (use check|scan|reset|unfreeze|status)" >&2
        exit 2
        ;;
esac
