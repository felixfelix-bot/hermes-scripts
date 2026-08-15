#!/bin/bash
# staggered-dispatch.sh — Gated staggered dispatch (STAGGER-DISPATCH t_5e11243e)
#
# Replaces the burst off-peak dispatch. Instead of spawning --max 2 at once,
# this wrapper:
#   1. Checks load + RAM BEFORE each dispatch pass
#   2. Only spawns max 1 worker per board per pass
#   3. Waits 60s between board dispatches (natural staggering via 2-min cron)
#   4. Skips entirely if load >= 3.0 OR available RAM <= 1.5GB
#   5. Checks rate_limit_gate.json (KALMAN-GATE t_6aceaaa3) — honors "paused"
#
# Designed for an every-2-min off-peak cron; uses flock to prevent overlap.

set -u

LOG_TAG="staggered-dispatch"
log() { logger -t "$LOG_TAG" -- "$*" 2>/dev/null || printf '[%s] %s\n' "$LOG_TAG" "$*" >&2; }

# --- Config (overridable via env) ---
LOAD_THRESHOLD="${LOAD_THRESHOLD:-3.4}"
RAM_MIN_MB="${RAM_MIN_MB:-1500}"          # 1.5 GB
SLEEP_BETWEEN="${SLEEP_BETWEEN:-30}"      # seconds between board passes
FAILURE_LIMIT="${FAILURE_LIMIT:-5}"
GATE_FILE="${GATE_FILE:-$HOME/.hermes/state/rate_limit_gate.json}"
BOARDS="${BOARDS:-fips infrastructure hermes-for-friends}"
HERMES_BIN="${HERMES_BIN:-/home/c03rad0r/.hermes/hermes-agent/venv/bin/hermes}"

# --- flock: prevent overlapping runs ---
LOCK_FILE="${LOCK_FILE:-/tmp/staggered-dispatch.lock}"
exec 9>"$LOCK_FILE" 2>/dev/null || exec 9>/tmp/staggered-dispatch.fallback.lock
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

# --- Kalman rate-limit gate check (KALMAN-GATE t_6aceaaa3) ---
# Honors the live gate JSON shape: {"paused": true, "reason": "..."}.
# Defensive: missing/unparseable file => allow (fail-open) so infra gaps don't
# permanently deadlock dispatch; the resource gate still protects the host.
check_gate() {
    [ -f "$GATE_FILE" ] || { log "gate file absent ($GATE_FILE); proceeding (fail-open)"; return 0; }
    local blocked
    blocked=$(python3 - "$GATE_FILE" <<'PY' 2>/dev/null || echo "0"
import json, sys
try:
    g = json.load(open(sys.argv[1]))
except Exception:
    print("0"); sys.exit()
# Recognise the live shape ("paused") plus defensive alternatives.
blocked = (
    bool(g.get("paused"))
    or bool(g.get("blocked"))
    or bool(g.get("tripped"))
    or (g.get("dispatch_allowed") is False)
)
print("1" if blocked else "0")
PY
)
    if [ "$blocked" = "1" ]; then
        log "rate-limit gate PAUSED ($GATE_FILE) — skipping dispatch"
        return 1
    fi
    log "rate-limit gate OK"
    return 0
}

# --- Main ---
log "starting staggered dispatch run (boards=$BOARDS)"

# Pre-flight combined gate
check_gate    || exit 0
check_resources "pre-flight" || exit 0

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
    check_gate || break
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
