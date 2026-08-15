#!/bin/bash
# staggered-dispatch.sh — Gated staggered dispatch (STAGGER-DISPATCH t_5e11243e)
#
# Replaces the burst off-peak dispatch. Instead of spawning --max 2 at once,
# this wrapper:
#   1. Checks load + RAM BEFORE each dispatch pass
#   2. Only spawns max 1 worker per board per pass
#   3. Waits between board dispatches (natural staggering via 2-min cron)
#   4. Skips entirely if load >= threshold OR available RAM too low
#   5. Checks rate_limit_gate.json (KALMAN-GATE t_6aceaaa3) — honors "paused"
#
# T3.2 (BOARD-PAUSE t_75b0e344): quota-aware pause + resume semantics.
#   - Gate paused with a quota-class reason (zai-* / ACTIVE 429 / QUOTA-WINDOW /
#     KALMAN — the exact set rate_limit_gate.py decide() emits): write
#     $STATE_DIR/board_pause_<board> for every managed board and SKIP the
#     board entirely — no claims, no promotes, no failure accounting. Tasks
#     sit ready, budgets untouched.
#   - Auto-resume: first clear pass removes the markers.
#   - Manager alert when a pause episode exceeds 2 h (once per episode).
#   - Fail-safe: a pause older than 6 h forces ONE canary claim (--max 1 on
#     the first paused board) so a stale gate can't freeze the board forever;
#     repeats at most every 6 h; alerts each time.
#   - Sweeper (--sweep mode, and automatically on a clear pass): tasks
#     blocked with reason prefix "quota-paused:" (the T3.3 worker taxonomy)
#     are unblocked via the hermes CLI — re-queued to ready, NOT counted as
#     failure — once the gate is confirmed clear.
#
# Marker JSON schema ($STATE_DIR/board_pause_<board>):
#   {board, paused_at_epoch, paused_at, updated_at_epoch, reason, resume_at,
#    alerted_2h, canary_at_epoch, canary_count}
#   paused_at_* are preserved across passes (episode start); reason/resume_at
#   track the latest gate state. Known race: if the dispatcher is down while
#     the gate clears and re-pauses, the marker inherits the old episode age
#     (worst case: an early alert or one extra canary — both bounded, safe).
#
# Fail-open rules (unchanged from T3.1): missing/unparseable gate file ->
# dispatch proceeds; an unknown pause reason still skips dispatch (any
# paused=true is fail-closed) but writes NO markers and sweeps nothing.
#
# Designed for an every-2-min off-peak cron; uses flock to prevent overlap.

set -u

LOG_TAG="staggered-dispatch"
log() {
    logger -t "$LOG_TAG" -- "$*" 2>/dev/null || true
    printf '[%s] %s\n' "$LOG_TAG" "$*" >&2
}
# Alerts go to syslog AND stdout (cron captures stdout in the dispatch log).
alert() {
    log "ALERT $*"
    printf 'ALERT %s\n' "$*"
}

# --- Config (overridable via env) ---
LOAD_THRESHOLD="${LOAD_THRESHOLD:-3.4}"
RAM_MIN_MB="${RAM_MIN_MB:-1500}"          # 1.5 GB
SLEEP_BETWEEN="${SLEEP_BETWEEN:-30}"      # seconds between board passes
FAILURE_LIMIT="${FAILURE_LIMIT:-5}"
STATE_DIR="${STATE_DIR:-$HOME/.hermes/state}"
GATE_FILE="${GATE_FILE:-$STATE_DIR/rate_limit_gate.json}"
BOARDS="${BOARDS:-fips infrastructure hermes-for-friends}"
HERMES_BIN="${HERMES_BIN:-/home/c03rad0r/.hermes/hermes-agent/venv/bin/hermes}"
KANBAN_BOARDS_ROOT="${KANBAN_BOARDS_ROOT:-$HOME/.hermes/kanban/boards}"
# Quota-class pause reasons — the exact top-level reason prefixes
# rate_limit_gate.py decide() emits for paused=true (docs/rate_limit_gate.md).
QUOTA_REASON_PREFIXES="${QUOTA_REASON_PREFIXES:-zai-:ACTIVE 429:QUOTA-WINDOW:KALMAN}"
PAUSE_ALERT_AFTER_S="${PAUSE_ALERT_AFTER_S:-7200}"    # manager alert >2 h
PAUSE_FAILSAFE_S="${PAUSE_FAILSAFE_S:-21600}"         # canary probe >=6 h
CANARY_INTERVAL_S="${CANARY_INTERVAL_S:-21600}"       # canary at most every 6 h

mkdir -p "$STATE_DIR" 2>/dev/null || true

# --- flock: prevent overlapping runs ---
# NOTE: the stderr suppression is scoped to the group — a bare
# `exec 9>... 2>/dev/null` would redirect fd 2 for the REST of the script
# (redirections on exec persist), swallowing every later log line.
LOCK_FILE="${LOCK_FILE:-/tmp/staggered-dispatch.lock}"
{ exec 9>"$LOCK_FILE"; } 2>/dev/null || { exec 9>/tmp/staggered-dispatch.fallback.lock; } 2>/dev/null
if ! flock -n 9; then
    log "another staggered-dispatch run is active; skipping"
    exit 0
fi

# --- Resource check helper (load + RAM) ---
check_resources() {
    local label="$1" load ram_avail load_ok ram_ok
    load=$(awk '{print $1}' /proc/loadavg)
    ram_avail=$(free -m | awk '/^Mem:/ {print $7}')
    [ -z "${ram_avail:-}" ] && ram_avail=0
    load_ok=$(awk -v l="$load" -v t="$LOAD_THRESHOLD" 'BEGIN{print (l+0 < t+0) ? 1 : 0}')
    ram_ok=$(awk -v r="$ram_avail" -v m="$RAM_MIN_MB" 'BEGIN{print (r+0 > m+0) ? 1 : 0}')
    log "resource check [$label]: load=$load ok=${load_ok}, avail_ram=${ram_avail}MB ok=${ram_ok}"
    if [ "$load_ok" != "1" ] || [ "$ram_ok" != "1" ]; then
        log "resource gate FAILED for [$label] (load=$load, ram=${ram_avail}MB) — stopping"
        return 1
    fi
    return 0
}

# --- Rate-limit gate state (T3.1 shape + T3.2 classification) ---
# Sets: GATE_KNOWN (1=file parsed), GATE_PAUSED (0/1), GATE_QUOTA (0/1),
#       GATE_REASON, GATE_RESUME_AT. Missing/unparseable -> KNOWN=0 (fail-open).
GATE_KNOWN=0; GATE_PAUSED=0; GATE_QUOTA=0; GATE_REASON=""; GATE_RESUME_AT=""
read_gate_state() {
    GATE_KNOWN=0; GATE_PAUSED=0; GATE_QUOTA=0; GATE_REASON=""; GATE_RESUME_AT=""
    if [ ! -f "$GATE_FILE" ]; then
        log "gate file absent ($GATE_FILE); proceeding (fail-open)"
        return 0
    fi
    local parsed
    parsed=$(python3 - "$GATE_FILE" "$QUOTA_REASON_PREFIXES" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    g = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
reason = str(g.get("reason") or "")
resume = g.get("resume_at")
resume = "" if resume is None else str(resume)
paused = bool(g.get("paused")) or bool(g.get("blocked")) or bool(g.get("tripped")) \
    or (g.get("dispatch_allowed") is False)
prefixes = [p for p in sys.argv[2].split(":") if p]
quota = any(reason.startswith(p) for p in prefixes)
# reason goes LAST so a literal tab inside it can't shift fields —
# `read -r a b c d` folds everything remaining into the final variable.
print(f"{1 if paused else 0}\t{1 if quota else 0}\t{resume}\t{reason}")
PY
)
    if [ -z "$parsed" ]; then
        log "gate file unparseable ($GATE_FILE); proceeding (fail-open)"
        return 0
    fi
    GATE_KNOWN=1
    IFS=$'\t' read -r GATE_PAUSED GATE_QUOTA GATE_RESUME_AT GATE_REASON <<< "$parsed"
}

# Binary gate verdict for the dispatch loop (unchanged contract).
check_gate() {
    read_gate_state
    if [ "$GATE_KNOWN" != "1" ]; then
        return 0
    fi
    if [ "$GATE_PAUSED" = "1" ]; then
        log "rate-limit gate PAUSED ($GATE_FILE) — skipping dispatch"
        return 1
    fi
    log "rate-limit gate OK"
    return 0
}

# --- T3.2: board-pause markers ------------------------------------------
# Upsert markers for all managed boards, evaluate 2h alert + 6h canary.
# Emits lines for bash to act on:
#   MARKER <board> <age_s> [new]
#   ALERT2H <board> <age_s>          (crossed 2h this pass — already committed)
#   CANARY <board> <age_s>           (at most one; canary already committed)
apply_pause_markers() {  # $1=reason $2=resume_at
    python3 - "$STATE_DIR" "$BOARDS" "$1" "$2" \
        "$PAUSE_ALERT_AFTER_S" "$PAUSE_FAILSAFE_S" "$CANARY_INTERVAL_S" <<'PY'
import json, os, sys, time

state_dir, boards_s, reason, resume_at, alert_s, failsafe_s, interval_s = sys.argv[1:8]
boards = boards_s.split()
alert_s, failsafe_s, interval_s = int(alert_s), int(failsafe_s), int(interval_s)
now = time.time()

# Global canary interval: the pause is gate-global, so the fail-safe may fire
# ONE claim per CANARY_INTERVAL_S across ALL boards — not per board marker.
latest_canary = None
for b in boards:
    p = os.path.join(state_dir, f"board_pause_{b}")
    if os.path.exists(p):
        try:
            c = json.load(open(p)).get("canary_at_epoch")
        except Exception:
            c = None
        if c is not None and (latest_canary is None or c > latest_canary):
            latest_canary = c
canary_cooled = latest_canary is None or (now - float(latest_canary)) >= interval_s

canary_board = None
for board in boards:
    path = os.path.join(state_dir, f"board_pause_{board}")
    m = None
    if os.path.exists(path):
        try:
            m = json.load(open(path))
        except Exception:
            m = None
    is_new = m is None
    if is_new:
        m = {
            "board": board,
            "paused_at_epoch": now,
            "paused_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "alerted_2h": False,
            "canary_at_epoch": None,
            "canary_count": 0,
        }
    m["updated_at_epoch"] = now
    m["reason"] = reason
    m["resume_at"] = resume_at or None
    age = int(now - float(m["paused_at_epoch"]))
    fire_alert = (not m["alerted_2h"]) and age >= alert_s
    if fire_alert:
        m["alerted_2h"] = True
    fire_canary = (
        canary_cooled
        and canary_board is None
        and age >= failsafe_s
        and (m["canary_at_epoch"] is None
             or now - float(m["canary_at_epoch"]) >= interval_s)
    )
    if fire_canary:
        m["canary_at_epoch"] = now
        m["canary_count"] = int(m.get("canary_count", 0)) + 1
        canary_board = board
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, path)
    print(f"MARKER {board} {age} {'new' if is_new else ''}".rstrip())
    if fire_alert:
        print(f"ALERT2H {board} {age}")
    if fire_canary:
        print(f"CANARY {board} {age}")
PY
}

remove_pause_markers() {
    local board removed=0
    for board in $BOARDS; do
        local marker="$STATE_DIR/board_pause_$board"
        if [ -f "$marker" ]; then
            rm -f "$marker"
            log "board-pause marker removed board=$board — auto-resume (gate clear)"
            removed=$((removed + 1))
        fi
    done
    [ "$removed" -gt 0 ] && log "board-pause auto-resume: $removed marker(s) removed"
    return 0
}

# --- T3.2: quota sweeper -------------------------------------------------
# Re-queue tasks blocked with reason prefix "quota-paused:" once the gate is
# confirmed clear. Unblocks via the hermes CLI (HERMES_KANBAN_DB pin — the
# same resolution --board uses in production) so events/comments/invariants
# go through the sanctioned path. Re-queue is NOT a failure.
sweep_board() {  # $1=board -> echoes count unblocked
    local board="$1" db="$KANBAN_BOARDS_ROOT/$1/kanban.db" ids
    [ -f "$db" ] || { echo 0; return 0; }
    ids=$(python3 - "$db" <<'PY' 2>/dev/null || echo ""
import json, sqlite3, sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    blocked = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status='blocked'")]
except sqlite3.OperationalError:
    print("")
    sys.exit(0)
out = []
for tid in blocked:
    row = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked','unblocked') "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if not row or row[0] != "blocked":
        continue
    try:
        reason = (json.loads(row[1]) or {}).get("reason") or ""
    except Exception:
        continue
    if str(reason).startswith("quota-paused:"):
        out.append(tid)
print("\n".join(out))
PY
)
    local count=0 id
    for id in $ids; do
        [ -z "$id" ] && continue
        if HERMES_KANBAN_DB="$db" "$HERMES_BIN" kanban unblock "$id" \
            --reason "quota-sweeper(T3.2): gate clear — re-queued automatically, not a task failure" >/dev/null 2>&1; then
            log "sweeper: unblocked $id on board=$board (quota-paused -> ready, failures unchanged)"
            count=$((count + 1))
        else
            log "sweeper: FAILED to unblock $id on board=$board (rc=$?) — will retry next clear pass"
        fi
    done
    echo "$count"
}

sweep_boards() {
    local board total=0 n
    for board in $BOARDS; do
        n="$(sweep_board "$board")"
        total=$((total + n))
    done
    if [ "$total" -gt 0 ]; then
        log "sweeper: $total task(s) re-queued across boards (quota-pause lifted)"
    else
        log "sweeper: nothing to re-queue"
    fi
}

# --- T3.2 pause episode handling (markers + alerts + canary) -------------
handle_quota_pause() {  # $1=reason $2=resume_at
    local tag board age _extra
    while IFS=' ' read -r tag board age _extra; do
        [ -z "${tag:-}" ] && continue
        case "$tag" in
            MARKER)
                log "board-pause ACTIVE board=$board age=${age}s reason=$1 resume_at=$2 — skip (no claims/promotes/failure accounting)"
                ;;
            ALERT2H)
                alert "board-paused >2h: board=$board paused ${age}s reason='$1' resume_at=$2 — manager attention requested (T3.2)"
                ;;
            CANARY)
                log "canary dispatch issued board=$board (age=${age}s >= ${PAUSE_FAILSAFE_S}s — probing stale gate)"
                alert "BOARD-PAUSE FAILSAFE: board=$board paused ${age}s >= 6h — forcing ONE canary claim to probe the gate; watch the canary task (T3.2)"
                "$HERMES_BIN" kanban --board "$board" dispatch --max 1 \
                    --failure-limit "$FAILURE_LIMIT" </dev/null 2>&1 || \
                    log "canary dispatch board=$board returned rc=$?"
                ;;
        esac
    done < <(apply_pause_markers "$1" "$2")
}

# --- Sweeper-only mode (--sweep) ------------------------------------------
if [ "${1:-}" = "--sweep" ]; then
    log "sweep-only run (boards=$BOARDS)"
    read_gate_state
    if [ "$GATE_KNOWN" = "1" ] && [ "$GATE_PAUSED" = "0" ]; then
        sweep_boards
    else
        log "sweep skipped — gate not confirmed clear (known=$GATE_KNOWN paused=$GATE_PAUSED)"
    fi
    exit 0
fi

# --- Main ---
log "starting staggered dispatch run (boards=$BOARDS)"

# Pre-flight combined gate (T3.2 classification happens here)
read_gate_state
if [ "$GATE_KNOWN" = "1" ] && [ "$GATE_PAUSED" = "1" ]; then
    if [ "$GATE_QUOTA" = "1" ]; then
        handle_quota_pause "$GATE_REASON" "$GATE_RESUME_AT"
        log "staggered dispatch run complete — board-pause in effect, no dispatch"
        exit 0
    fi
    log "rate-limit gate PAUSED with unknown pause reason '$GATE_REASON' — skipping dispatch (no markers: reason outside zai-*/429/quota/kalman contract)"
    exit 0
fi
# Gate clear (or fail-open unknown): auto-resume + sweep, then dispatch.
# Sweep only runs on a CONFIRMED clear gate — fail-open does not re-queue.
if [ "$GATE_KNOWN" = "1" ]; then
    remove_pause_markers
    sweep_boards
    log "rate-limit gate OK"
else
    log "gate state unknown (fail-open) — no marker removal, no sweep"
fi
check_resources "pre-flight" || exit 0

# Emergency stop: dispatch freeze marker blocks ALL dispatch (SOUL contract).
if [ -f "$HOME/.hermes/bot/.dispatch_frozen" ]; then
    log "dispatch FROZEN (.dispatch_frozen present) — skipping pass"
    exit 0
fi

# Dispatch loop — one board per pass, re-check gate + resources before each spawn
spawned=0
for board in $BOARDS; do
    # Circuit-breaker pre-spawn check (D3): exit 1 = HOLD (skip this board only).
    # Any other rc — internal error (2+) or missing breaker (127) — fails OPEN
    # per the circuit-breaker.sh contract: never wedge dispatch on our own bugs.
    cb_rc=0
    cb_msg=$(bash "$HOME/.hermes/scripts/circuit-breaker.sh" check "$board" 2>/dev/null) || cb_rc=$?
    if [ "$cb_rc" -eq 1 ]; then
        log "circuit breaker HOLD board=$board (${cb_msg:-frozen or tripped}) — skipping board"
        continue
    fi
    if ! check_gate; then
        # Gate tripped mid-pass: if quota-class, persist markers for ALL boards
        # so the pause is visible + fail-safe armed even if the next cron is missed.
        if [ "$GATE_KNOWN" = "1" ] && [ "$GATE_PAUSED" = "1" ] && [ "$GATE_QUOTA" = "1" ]; then
            handle_quota_pause "$GATE_REASON" "$GATE_RESUME_AT"
        fi
        break
    fi
    check_resources "$board" || break
    log "dispatching board=$board max=1 failure-limit=$FAILURE_LIMIT"
    if "$HERMES_BIN" kanban --board "$board" dispatch --max 1 --failure-limit "$FAILURE_LIMIT" 2>&1; then
        spawned=$((spawned + 1))
    else
        log "dispatch board=$board returned rc=$? (nothing-ready or transient) — continuing"
    fi
    sleep "$SLEEP_BETWEEN"
done

log "staggered dispatch complete — $spawned board pass(es) issued"
exit 0
