#!/usr/bin/env bash
# ppq_logger_wrapper.sh — Robust wrapper for ppq_logger.py
#
# Handles cron environment issues and provides comprehensive error handling

set -euo pipefail

# Ensure we're in the correct directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Set up logging
LOG_DIR="/tmp/ppq_logger_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/ppq_logger_$(date +%Y%m%d_%H%M%S).log"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Function to check if another instance is running
check_lock() {
    LOCK_FILE="/tmp/ppq_logger.lock"
    if [ -f "$LOCK_FILE" ]; then
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log "Another instance is already running (PID: $pid)"
            exit 1
        else
            log "Stale lock file found, removing it"
            rm -f "$LOCK_FILE"
        fi
    fi
    
    # Create new lock file
    echo $$ > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT
}

# Function to clean up old log files
cleanup_logs() {
    find "$LOG_DIR" -name "ppq_logger_*.log" -type f -mtime +7 -delete 2>/dev/null || true
}

# Main execution
main() {
    log "=== PPQ Logger Wrapper Starting ==="
    
    # Check for running instance
    check_lock
    
    # Clean up old logs
    cleanup_logs
    
    # Set environment variables that might be missing in cron
    export PPQ_API_KEY="${PPQ_API_KEY:-}"
    export HOME="${HOME:-/home/c03rad0r}"
    export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"
    
    # Verify Python is available
    if ! command -v python3 >/dev/null 2>&1; then
        log "ERROR: python3 not found in PATH"
        exit 1
    fi
    
    # Verify the main script exists and is executable
    if [ ! -f "ppq_logger.py" ]; then
        log "ERROR: ppq_logger.py not found in $SCRIPT_DIR"
        exit 1
    fi
    
    # Run the main script with error handling
    log "Starting ppq_logger.py"
    
    if python3 "$SCRIPT_DIR/ppq_logger.py" 2>&1 | tee -a "$LOG_FILE"; then
        log "=== PPQ Logger Completed Successfully ==="
        exit 0
    else
        rc=$?
        log "=== PPQ Logger Failed (exit code: $rc) ==="
        
        # Send error notification if possible
        if [ -n "${SLACK_WEBHOOK:-}" ] || [ -n "${ERROR_EMAIL:-}" ]; then
            ERROR_MSG="PPQ logger failed at $(date). Exit code: $rc. Log: $LOG_FILE"
            # You can add your notification logic here
            log "Would send error notification: $ERROR_MSG"
        fi
        
        exit $rc
    fi
}

# Run main function
main "$@"