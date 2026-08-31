#!/usr/bin/env bash
# safe-single-dispatch.sh — Ultra-conservative dispatch: max 1 task, extended monitoring
#
# Replaces the burst dispatch with a single-task dispatcher that:
#   1. Only dispatches 1 task maximum per run
#   2. Has extended resource monitoring before dispatch
#   3. Waits 60 seconds after successful dispatch
#   4. Blocks if system resources are constrained
#   5. Designed to run every 2-3 minutes via cron

set -u

LOG_TAG="safe-single-dispatch"
log() { 
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $LOG_TAG: $*" | tee -a /tmp/safe-single-dispatch.log
    logger -t "$LOG_TAG" -- "$*" 2>/dev/null || printf '[%s] %s\n' "$LOG_TAG" "$*" >&2; 
}

# --- Config (very conservative) ---
LOAD_THRESHOLD="${LOAD_THRESHOLD:-2.0}"        # Lower than normal
RAM_MIN_MB="${RAM_MIN_MB:-2000}"             # 2 GB minimum (higher than normal)
FAIL_LIMIT="${FAIL_LIMIT:-2}"               # Very conservative failure limit
MAX_SINGLE_TASK="${MAX_SINGLE_TASK:-1}"      # NEVER more than 1 task per run
BOARD="${BOARD:-fips}"                       # Only dispatch to FIPS board
WAIT_AFTER_DISPATCH="${WAIT_AFTER_DISPATCH:-60}"  # Wait 60s after dispatch

# --- Resource check helper ---
check_resources() {
    local load ram_avail load_ok ram_ok
    load=$(awk '{print $1}' /proc/loadavg)
    ram_avail=$(free -m | awk '/^Mem:/ {print $7}')
    [ -z "${ram_avail:-}" ] && ram_avail=0
    load_ok=$(awk -v l="$load" -v t="$LOAD_THRESHOLD" 'BEGIN{print (l+0 < t+0) ? 1 : 0}')
    ram_ok=$(awk -v r="$ram_avail" -v m="$RAM_MIN_MB" 'BEGIN{print (r+0 > m+0) ? 1 : 0}')
    
    log "resource check: load=$load (threshold=$LOAD_THRESHOLD) ok=${load_ok}, avail_ram=${ram_avail}MB (min=$RAM_MIN_MB) ok=${ram_ok}"
    
    if [ "$load_ok" != "1" ] || [ "$ram_ok" != "1" ]; then
        log "resource gate FAILED (load=$load, ram=${ram_avail}MB) — stopping"
        return 1
    fi
    return 0
}

# --- Check if there are already too many running tasks ---
check_running_tasks() {
    local running_count
    running_count=$(hermes kanban --board "$BOARD" list 2>/dev/null | grep -c "running" || echo 0)
    log "current running tasks on $BOARD: $running_count"
    
    # If there are already 3+ running tasks, don't dispatch more
    if [ "$running_count" -ge 3 ]; then
        log "too many running tasks ($running_count >= 3) — skipping dispatch"
        return 1
    fi
    
    return 0
}

# --- Main ---
log "starting safe single-dispatch run (board=$BOARD, max_tasks=$MAX_SINGLE_TASK)"

# Pre-flight resource check
if ! check_resources; then
    log "resource check failed — exiting"
    exit 0
fi

# Check running task count
if ! check_running_tasks; then
    log "running task check failed — exiting"
    exit 0
fi

# Dispatch with very conservative limits
log "dispatching board=$BOARD max=$MAX_SINGLE_TASK failure-limit=$FAIL_LIMIT"

if hermes kanban --board "$BOARD" dispatch --max "$MAX_SINGLE_TASK" --failure-limit "$FAIL_LIMIT" 2>&1 | tee -a /tmp/safe-single-dispatch.log; then
    log "dispatch successful — waiting ${WAIT_AFTER_DISPATCH}s before next run"
    sleep "$WAIT_AFTER_DISPATCH"
else
    rc=$?
    log "dispatch returned rc=$rc (likely no ready tasks or transient error) — continuing"
fi

log "safe single-dispatch complete"
exit 0