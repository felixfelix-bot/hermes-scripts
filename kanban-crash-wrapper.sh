#!/usr/bin/env bash
# ============================================================================
# kanban-crash-wrapper.sh
#
# Supervises kanban worker processes. When the worker dies abnormally (OOM
# kill, segfault, signal), captures system diagnostics and posts a summary
# as a kanban comment on the task thread.
#
# PROBLEM: 4 of 6 blocked kanban tasks died with "pid not alive" — zero
# diagnostics captured. The dispatcher's detect_crashed_workers() runs on the
# NEXT tick (60s later), by which time the process is gone and OOM evidence
# has scrolled out of dmesg. This wrapper sits BETWEEN the dispatcher and the
# worker so it can capture evidence at the exact moment of death.
#
# ARCHITECTURE:
#   dispatcher (gateway/timer)
#     └─ Popen(["kanban-crash-wrapper.sh", "-p", profile, ..., "chat", "-q", ...])
#          └─ supervisor (bash, this script) — PID recorded in DB
#               └─ hermes -p profile ... chat -q "work kanban task X"
#                    └─ (crashes: OOM / segfault / signal)
#               └─ wait → capture dmesg/journalctl/free → post comment → exit
#
# ACTIVATION: set HERMES_BIN to this script in the dispatcher's environment.
#   _resolve_hermes_argv() in kanban_db.py checks $HERMES_BIN first.
#
#   Gateway (systemd):
#     Environment="HERMES_BIN=%h/.hermes/scripts/kanban-crash-wrapper.sh"
#   throttled_daemon.sh:
#     export HERMES_BIN="$HOME/.hermes/scripts/kanban-crash-wrapper.sh"
#
# Non-kanban invocations pass through transparently (exec hermes directly).
#
# DIAGNOSTIC LOGS: /tmp/worker-crash-{PID}-{TIMESTAMP}.log
# ============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Resolve the REAL hermes binary (not ourselves)
# ---------------------------------------------------------------------------
_self="$(readlink -f "$0" 2>/dev/null || echo "$0")"
_real_hermes=""

# Strategy 1: explicit override
if [[ -n "${HERMES_REAL_BIN:-}" && -x "${HERMES_REAL_BIN:-}" ]]; then
    _resolved="$(readlink -f "$HERMES_REAL_BIN" 2>/dev/null || echo "$HERMES_REAL_BIN")"
    [[ "$_resolved" != "$_self" ]] && _real_hermes="$_resolved"
fi

# Strategy 2: search PATH for 'hermes', skipping ourselves
if [[ -z "$_real_hermes" ]]; then
    while IFS= read -r _candidate; do
        [[ -z "$_candidate" ]] && continue
        _resolved="$(readlink -f "$_candidate" 2>/dev/null || echo "$_candidate")"
        if [[ "$_resolved" != "$_self" && -x "$_resolved" ]]; then
            _real_hermes="$_resolved"
            break
        fi
    done < <({ command -v hermes 2>/dev/null; type -ap hermes 2>/dev/null; } | sort -u)
fi

# Strategy 3: known locations
if [[ -z "$_real_hermes" ]]; then
    for _try in \
        "$HOME/.hermes/hermes-agent/venv/bin/hermes" \
        "$HOME/.local/bin/hermes" \
        "/usr/local/bin/hermes" \
        "/usr/bin/hermes"; do
        _resolved="$(readlink -f "$_try" 2>/dev/null || echo "$_try")"
        if [[ "$_resolved" != "$_self" && -x "$_resolved" ]]; then
            _real_hermes="$_resolved"
            break
        fi
    done
fi

if [[ -z "$_real_hermes" ]]; then
    echo "FATAL: kanban-crash-wrapper cannot find the real hermes binary" >&2
    echo "       Set HERMES_REAL_BIN or ensure 'hermes' is on PATH" >&2
    exit 127
fi

# ---------------------------------------------------------------------------
# Fleet admission cap (OPERATOR 2026-09-11): env override, lowered by an
# optional throttle file written by fleet_remediate.py (action=throttle).
# ---------------------------------------------------------------------------
_FLEET_CAP="${HERMES_FLEET_CAP:-4}"
_FLEET_CAP_FILE="$HOME/.hermes/bot/.fleet_cap"
if [[ -f "$_FLEET_CAP_FILE" ]]; then
    _ffcap="$(grep -oE '"cap"[[:space:]]*:[[:space:]]*[0-9]+' "$_FLEET_CAP_FILE" 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    if [[ -n "${_ffcap:-}" && "$_ffcap" -ge 0 && "$_ffcap" -lt "$_FLEET_CAP" ]]; then
        _FLEET_CAP="$_ffcap"
    fi
fi

# ---------------------------------------------------------------------------
# Passthrough for non-kanban invocations (transparent), with universal
# admission (OPERATOR 2026-09-11, H.2) for AUTOMATED worker-profile runs.
#
# Cron/script-spawned worker sessions (`hermes -p worker-… chat -q …`) used to
# bypass the fleet gate entirely. Gate only the non-interactive query form
# (`-q`/`--query`) so interactive operator runs and non-worker profiles still
# pass straight through via exec. The reserved slot's PID is preserved across
# exec, and dead-PID slots are pruned under the lock, so no slot leaks.
# ---------------------------------------------------------------------------
_ARGS=("$@")
_NARGS=${#_ARGS[@]}
_PROF=""
_HAS_QUERY=0
for (( _i=0; _i<_NARGS; _i++ )); do
    case "${_ARGS[_i]}" in
        -p|--profile) [[ $((_i+1)) -lt $_NARGS ]] && _PROF="${_ARGS[$((_i+1))]}" ;;
        -p=*|--profile=*) _PROF="${_ARGS[_i]#*=}" ;;
        -q|--query) _HAS_QUERY=1 ;;
    esac
done

if [[ -z "${HERMES_KANBAN_TASK:-}" ]]; then
    if [[ "$_PROF" == worker-* && "$_HAS_QUERY" -eq 1 ]]; then
        _GATE_LOCK="$HOME/.hermes/bot/.fleet_gate.lock"
        _SLOT_DIR="$HOME/.hermes/bot/.fleet_slots"
        mkdir -p "$_SLOT_DIR" 2>/dev/null || true
        _gate_reason=""
        exec 9>"$_GATE_LOCK" 2>/dev/null || true
        if [[ -w "$_GATE_LOCK" ]] && flock -w 15 9; then
            for _sf in "$_SLOT_DIR"/*.slot; do
                [[ -e "$_sf" ]] || continue
                _sp="$(basename "$_sf" .slot)"
                kill -0 "$_sp" 2>/dev/null || rm -f "$_sf"
            done
            _live=$(find "$_SLOT_DIR" -maxdepth 1 -name '*.slot' 2>/dev/null | wc -l)
            if [[ -e "$HOME/.hermes/ESTOP" ]]; then
                _gate_reason="estop"
            elif [[ -e "$HOME/.hermes/bot/.dispatch_frozen" ]]; then
                _gate_reason="dispatch_frozen"
            elif [[ -e "$HOME/.hermes/bot/.fleet_quarantine" ]]; then
                _gate_reason="quarantine"
            elif [[ "${_live:-0}" -ge "$_FLEET_CAP" ]]; then
                _gate_reason="fleet_cap(${_live}>=${_FLEET_CAP})"
            else
                : > "$_SLOT_DIR/$$.slot" 2>/dev/null || true
            fi
            flock -u 9
        else
            _gate_reason="gate_lock_unavailable"
        fi
        exec 9>&- 2>/dev/null || true
        if [[ -n "$_gate_reason" ]]; then
            printf '%s GATED profile=%s reason=%s wrapper=%s ppid=%s parent="%s"\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_PROF" "$_gate_reason" "$$" "$PPID" \
                "$(tr '\0' ' ' < "/proc/${PPID}/cmdline" 2>/dev/null | cut -c1-200)" \
                >> "$HOME/.hermes/logs/kanban-spawn-audit.log" 2>/dev/null || true
            echo "FLEET GATE (worker profile): ${_gate_reason} — refusing ${_PROF} run (no spend)." >&2
            exit 75
        fi
    fi
    exec "$_real_hermes" "$@"
fi

# ---------------------------------------------------------------------------
# Kanban worker supervision
# ---------------------------------------------------------------------------
_TASK_ID="$HERMES_KANBAN_TASK"
_WRAPPER_PID=$$
_START_EPOCH=$(date +%s)
_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
_SPAWN_LOG="$HOME/.hermes/logs/kanban-spawn-audit.log"

# ---------------------------------------------------------------------------
# Fleet gate (operator 2026-09-11) — bounds EVERY spawn path that resolves
# through HERMES_BIN, including per-board CLI `kanban dispatch` loops and
# reclaim/respawn bursts that bypass the embedded dispatcher's global cap.
# Refuses via EX_TEMPFAIL (75) so the dispatcher releases the task back to
# `ready` WITHOUT counting a failure (no crash-loop, no token burn).
#   (a) hard freeze:  ~/.hermes/bot/.dispatch_frozen
#   (b) live-worker cap: $HERMES_FLEET_CAP (default 4)
# Also writes a spawner-identity audit line (ppid + parent cmdline) so a
# future storm is attributable to the mechanism that caused it.
# ---------------------------------------------------------------------------
_parent_cmd="$(tr '\0' ' ' < "/proc/${PPID}/cmdline" 2>/dev/null | cut -c1-200)"

# ---------------------------------------------------------------------------
# Atomic admission (OPERATOR 2026-09-11, H.1): the previous cap check ran
# `pgrep -f` OUTSIDE any lock, so N concurrent per-board dispatch loops all
# read a stale low count and passed before their siblings registered (TOCTOU)
# — the ~70-worker storm. It was also fooled by ANY process whose cmdline
# merely mentioned the script name. Replace with an flock-serialized,
# self-healing PID-slot directory: each admitted wrapper drops a
# ``<pid>.slot``; the count (dead PIDs pruned) is authoritative because every
# admitted wrapper is itself visible to the next.
# ---------------------------------------------------------------------------
_GATE_LOCK="$HOME/.hermes/bot/.fleet_gate.lock"
_SLOT_DIR="$HOME/.hermes/bot/.fleet_slots"
mkdir -p "$_SLOT_DIR" 2>/dev/null || true

_cleanup_slot() {
    rm -f "$_SLOT_DIR/$_WRAPPER_PID.slot" 2>/dev/null || true
    rm -f "${_SPAWN_MARK:-/nonexistent}" 2>/dev/null || true
}
trap '_cleanup_slot' EXIT

_gate_reason=""
_live_siblings=0
exec 9>"$_GATE_LOCK" 2>/dev/null || true
if [[ -w "$_GATE_LOCK" ]] && flock -w 15 9; then
    # Prune slots whose PID is gone (wrapper was SIGKILLed / crashed).
    for _sf in "$_SLOT_DIR"/*.slot; do
        [[ -e "$_sf" ]] || continue
        _sp="$(basename "$_sf" .slot)"
        kill -0 "$_sp" 2>/dev/null || rm -f "$_sf"
    done
    _live_siblings=$(find "$_SLOT_DIR" -maxdepth 1 -name '*.slot' 2>/dev/null | wc -l)
    if [[ -e "$HOME/.hermes/ESTOP" ]]; then
        _gate_reason="estop"
    elif [[ -e "$HOME/.hermes/bot/.dispatch_frozen" ]]; then
        _gate_reason="dispatch_frozen"
    elif [[ -e "$HOME/.hermes/bot/.fleet_quarantine" ]]; then
        _gate_reason="quarantine"
    elif [[ "${_live_siblings:-0}" -ge "$_FLEET_CAP" ]]; then
        _gate_reason="fleet_cap(${_live_siblings}>=${_FLEET_CAP})"
    else
        # Reserve our slot while still holding the lock.
        : > "$_SLOT_DIR/$_WRAPPER_PID.slot" 2>/dev/null || true
        _live_siblings=$(( _live_siblings + 1 ))
    fi
    flock -u 9
else
    # Fail safe: if the lock cannot be taken, refuse this spawn rather than
    # risk adding to an unbounded burst.
    _gate_reason="gate_lock_unavailable"
fi
exec 9>&- 2>/dev/null || true

if [[ -n "$_gate_reason" ]]; then
    {
        printf '%s GATED task=%s board=%s reason=%s wrapper=%s ppid=%s parent="%s"\n' \
            "$_START_TS" "$_TASK_ID" "${HERMES_KANBAN_BOARD:-?}" "$_gate_reason" \
            "$_WRAPPER_PID" "$PPID" "$_parent_cmd"
    } >> "$_SPAWN_LOG" 2>/dev/null || true
    echo "FLEET GATE: ${_gate_reason} — releasing task ${_TASK_ID} to ready (no spend)." >&2
    exit 75
fi

# Spawner audit (every allowed spawn) — identifies the dispatch mechanism.
# Idempotence guard (I.7): a rare double-invocation under one PID previously
# wrote two identical SPAWN lines; log at most once per wrapper process.
_SPAWN_MARK="${TMPDIR:-/tmp}/.fleet_spawn_${_WRAPPER_PID}"
if [[ ! -e "$_SPAWN_MARK" ]]; then
    {
        printf '%s SPAWN task=%s board=%s wrapper=%s ppid=%s live=%s parent="%s"\n' \
            "$_START_TS" "$_TASK_ID" "${HERMES_KANBAN_BOARD:-?}" "$_WRAPPER_PID" \
            "$PPID" "${_live_siblings:-0}" "$_parent_cmd"
    } >> "$_SPAWN_LOG" 2>/dev/null || true
    : > "$_SPAWN_MARK" 2>/dev/null || true
fi

# Launch hermes as a child process
"$_real_hermes" "$@" &
_CHILD_PID=$!

# Forward termination signals to the child
_forward_signal() {
    local _sig="$1"
    kill -"$_sig" "$_CHILD_PID" 2>/dev/null || true
}
trap '_forward_signal TERM' TERM
trap '_forward_signal INT'  INT
trap '_forward_signal HUP'  HUP

# Wait for the child to exit
wait "$_CHILD_PID" 2>/dev/null
_EXIT_CODE=$?

# Exit code 75 = EX_TEMPFAIL (rate-limited / quota wall) — not a crash
if [[ $_EXIT_CODE -eq 0 || $_EXIT_CODE -eq 75 ]]; then
    exit $_EXIT_CODE
fi

# ---------------------------------------------------------------------------
# Abnormal exit: capture diagnostics
# ---------------------------------------------------------------------------
_CRASH_TS=$(date +%Y%m%d-%H%M%S)
_LOG_FILE="/tmp/worker-crash-${_CHILD_PID}-${_CRASH_TS}.log"
_END_EPOCH=$(date +%s)
_DURATION=$(( _END_EPOCH - _START_EPOCH ))

# Signal interpretation
if [[ $_EXIT_CODE -gt 128 ]]; then
    _SIG=$(( _EXIT_CODE - 128 ))
    _SIG_NAME="$(kill -l "$_SIG" 2>/dev/null || echo "signal-$_SIG")"
    _EXIT_DESC="killed by signal $_SIG ($_SIG_NAME)"
    if [[ "$_SIG" -eq 9 ]]; then
        _EXIT_DESC="$_EXIT_DESC — likely OOM kill or SIGKILL"
    elif [[ "$_SIG" -eq 11 ]]; then
        _EXIT_DESC="$_EXIT_DESC — segfault"
    elif [[ "$_SIG" -eq 6 ]]; then
        _EXIT_DESC="$_EXIT_DESC — abort/assertion failure"
    fi
else
    _EXIT_DESC="exited with code $_EXIT_CODE"
fi

{
    echo "========================================"
    echo "  WORKER CRASH DIAGNOSTIC CAPTURE"
    echo "========================================"
    echo "Task ID:        $_TASK_ID"
    echo "Worker PID:     $_CHILD_PID  (supervisor PID: $_WRAPPER_PID)"
    echo "Profile:        ${HERMES_PROFILE:-unknown}"
    echo "Workspace:      ${HERMES_KANBAN_WORKSPACE:-unknown}"
    echo "Board:          ${HERMES_KANBAN_BOARD:-unknown}"
    echo "Run ID:         ${HERMES_KANBAN_RUN_ID:-unknown}"
    echo "Exit:           $_EXIT_DESC"
    echo "Exit code:      $_EXIT_CODE"
    echo "Started:        $_START_TS"
    echo "Ended:          $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Duration:       ${_DURATION}s"
    echo "Real hermes:    $_real_hermes"
    echo ""

    echo "--- free memory at crash time ---"
    free -h 2>/dev/null || echo "(free not available)"
    echo ""

    echo "--- /proc/meminfo (head) ---"
    head -8 /proc/meminfo 2>/dev/null || echo "(not available)"
    echo ""

    echo "--- dmesg: OOM / kill / segfault (last 40 matches) ---"
    # dmesg requires root on most systems; try sudo, then bare
    _dmesg_out=""
    _dmesg_out="$(dmesg 2>/dev/null)" || _dmesg_out="$(sudo dmesg 2>/dev/null)" || true
    if [[ -n "$_dmesg_out" ]]; then
        echo "$_dmesg_out" | grep -iE "oom|killed process|out.of.memory|segfault|$_CHILD_PID" | tail -40 || echo "(no OOM/kill/segfault matches in dmesg)"
    else
        echo "(dmesg not accessible — try: sudo dmesg --follow in a terminal)"
    fi
    echo ""

    echo "--- dmesg: exact OOM kill for PID $_CHILD_PID ---"
    if [[ -n "$_dmesg_out" ]]; then
        echo "$_dmesg_out" | grep -B2 -A10 "Killed process.*$_CHILD_PID\|Out of memory.*$_CHILD_PID\|oom_reaper.*$_CHILD_PID\|oom-kill.*$_CHILD_PID" || echo "(no exact OOM match for PID $_CHILD_PID)"
    else
        echo "(dmesg not accessible)"
    fi
    echo ""

    echo "--- journalctl for PID $_CHILD_PID (last 80 lines) ---"
    journalctl _PID="$_CHILD_PID" --no-pager -n 80 2>/dev/null || echo "(journalctl not available or no entries for PID $_CHILD_PID)"
    echo ""

    echo "--- journalctl: recent system warnings (last 30, filtered) ---"
    journalctl --no-pager -n 30 -p warning --since "-5 min" 2>/dev/null | grep -iE "oom|kill|memory|out.of|segfault|signal" || echo "(no relevant system warnings in last 5 min)"
    echo ""

    echo "--- resource limits (ulimit -a) ---"
    bash -c 'ulimit -a' 2>/dev/null || echo "(not available)"
    echo ""

    echo "--- cgroup memory info ---"
    _cg_file="/proc/$_CHILD_PID/cgroup"
    if [[ -f "$_cg_file" ]]; then
        echo "cgroup memberships:"
        cat "$_cg_file" 2>/dev/null
        # Try cgroup v2 memory events
        _cg_path="$(cat "$_cg_file" 2>/dev/null | sed -n 's/^0::\(.*\)/\1/p' | head -1)"
        if [[ -n "$_cg_path" ]]; then
            for _base in "/sys/fs/cgroup$_cg_path" "/sys/fs/cgroup/$_cg_path"; do
                if [[ -f "$_base/memory.events" ]]; then
                    echo ""
                    echo "cgroup v2 memory.events at $_base:"
                    cat "$_base/memory.events" 2>/dev/null
                    echo ""
                    echo "memory.current: $(cat "$_base/memory.current" 2>/dev/null || echo '?')"
                    echo "memory.max: $(cat "$_base/memory.max" 2>/dev/null || echo '?')"
                    break
                fi
            done
        fi
    else
        echo "(process already reaped, /proc/$_CHILD_PID/cgroup not readable)"
        # Try our own cgroup as fallback
        _cg_self="/proc/$$/cgroup"
        if [[ -f "$_cg_self" ]]; then
            echo "supervisor cgroup:"
            cat "$_cg_self" 2>/dev/null
        fi
    fi
    echo ""

    echo "--- worker log tail (last 30 lines from board log) ---"
    _kanban_db="${HERMES_KANBAN_DB:-}"
    if [[ -n "$_kanban_db" ]]; then
        _board_root="$(dirname "$_kanban_db" 2>/dev/null)"
        _worker_log="$_board_root/logs/${_TASK_ID}.log"
        if [[ -f "$_worker_log" ]]; then
            tail -30 "$_worker_log" 2>/dev/null || echo "(could not read $_worker_log)"
        else
            echo "(worker log not found at $_worker_log)"
        fi
    else
        echo "(HERMES_KANBAN_DB not set, cannot locate worker log)"
    fi
    echo ""

    echo "--- /proc/$_CHILD_PID status (if still readable) ---"
    if [[ -f "/proc/$_CHILD_PID/status" ]]; then
        grep -E "^(Name|State|VmPeak|VmRSS|VmSwap|Threads):" "/proc/$_CHILD_PID/status" 2>/dev/null || echo "(not readable)"
    else
        echo "(process already reaped)"
    fi
    echo ""

    echo "========================================"
    echo "  END DIAGNOSTIC CAPTURE"
    echo "========================================"
} > "$_LOG_FILE" 2>&1

# ---------------------------------------------------------------------------
# Post summary as kanban comment (direct SQLite — most reliable)
# ---------------------------------------------------------------------------
_DB_PATH="${HERMES_KANBAN_DB:-}"
if [[ -n "$_DB_PATH" && -f "$_DB_PATH" ]]; then
    _AUTHOR="${HERMES_PROFILE:-crash-wrapper}"
    _python="$(command -v python3 2>/dev/null || echo "$HOME/.hermes/hermes-agent/venv/bin/python")"

    "$_python" - "$_DB_PATH" "$_TASK_ID" "$_AUTHOR" "$_LOG_FILE" "$_EXIT_DESC" "$_DURATION" "$_CHILD_PID" <<'PYEOF' 2>/dev/null || true
import os, sqlite3, sys, time

db_path, task_id, author, log_file, exit_desc, duration, child_pid = sys.argv[1:8]

# Read the diagnostic log and extract key signals for the comment
try:
    with open(log_file) as f:
        log_content = f.read()
except Exception:
    log_content = ""

# Extract the most diagnostic lines for a concise comment
key_lines = []
for line in log_content.split("\n"):
    low = line.lower()
    if any(kw in low for kw in [
        "oom", "killed process", "out of memory", "segfault",
        "killed by signal", "memory.current", "memory.events",
        "oom-kill", "exited with code", "vmrss", "vmswap",
        "duration:", "exit:", "task id:",
    ]):
        stripped = line.strip()
        if stripped and len(stripped) < 200:
            key_lines.append(stripped)

# Determine likely cause — check exit_desc FIRST (more reliable than log text,
# which contains section headers with words like "segfault" / "oom")
exit_lower = exit_desc.lower()
log_lower = log_content.lower()
# Only look at dmesg/journalctl OUTPUT lines, not section headers
has_oom_evidence = bool([
    l for l in log_content.split("\n")
    if l.strip()
    and not l.startswith("---")
    and ("out of memory" in l.lower() or "oom-kill" in l.lower() or "killed process" in l.lower())
])
has_segfault_evidence = bool([
    l for l in log_content.split("\n")
    if l.strip()
    and not l.startswith("---")
    and "segfault" in l.lower()
])

if has_oom_evidence:
    cause = "OOM kill (confirmed in dmesg/journal)"
elif "signal 9" in exit_lower or "sigkill" in exit_lower:
    cause = "SIGKILL (likely OOM)"
elif has_segfault_evidence:
    cause = "segfault (confirmed in dmesg/journal)"
elif "signal 11" in exit_lower or "segfault" in exit_lower:
    cause = "segfault"
elif "signal 6" in exit_lower or "sigabrt" in exit_lower:
    cause = "abort/assertion"
else:
    cause = "non-zero exit"

summary = (
    f"**[CRASH CAPTURED]** by `kanban-crash-wrapper`\n\n"
    f"- **Exit:** {exit_desc}\n"
    f"- **Likely cause:** {cause}\n"
    f"- **Duration:** {duration}s\n"
    f"- **PID:** {child_pid}\n"
    f"- **Diagnostic log:** `{log_file}`\n"
)
if key_lines:
    summary += "\n**Key signals:**\n```\n"
    summary += "\n".join(key_lines[:12])
    summary += "\n```"

summary += f"\n_Full diagnostics at `{log_file}` — check `dmesg` and `journalctl` sections for OOM evidence._"

try:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, f"{author} [crash-wrapper]", summary, int(time.time())),
    )
    conn.commit()
    conn.close()
    print(f"[crash-wrapper] posted diagnostic comment to task {task_id}", file=sys.stderr)
except Exception as e:
    print(f"[crash-wrapper] FAILED to post comment: {e}", file=sys.stderr)
PYEOF
else
    echo "[crash-wrapper] WARNING: HERMES_KANBAN_DB not set or DB missing — skipping comment post" >&2
fi

echo "[crash-wrapper] crash diagnostics for task $_TASK_ID saved to $_LOG_FILE" >&2

# Exit with the child's exit code so the dispatcher sees the correct status
exit "$_EXIT_CODE"
