#!/bin/bash
# Memory pressure reminder — alerts if memory usage is still high
# Silent when pressure is gone. No timeout — keeps nagging until resolved.

MARKER="/tmp/mem-pressure-was-high"

MEM_INFO=$(free -m 2>/dev/null)
MEM_TOTAL=$(echo "$MEM_INFO" | awk '/^Mem:/ {print $2}')
MEM_USED=$(echo "$MEM_INFO" | awk '/^Mem:/ {print $3}')
MEM_PCT=$((MEM_USED * 100 / MEM_TOTAL))
SWAP_USED=$(echo "$MEM_INFO" | awk '/^Swap:/ {print $3}')

if [ $MEM_PCT -gt 70 ] || [ $SWAP_USED -gt 3000 ]; then
    # High pressure — remind
    echo "⚠️ Memory still under pressure: RAM ${MEM_PCT}%, swap ${SWAP_USED}MB."
    echo "Close opencode sessions: pkill -f opencode"
    echo "Check top hogs: ps aux --sort=-%mem | head -10"
    touch "$MARKER"
else
    # Pressure resolved
    if [ -f "$MARKER" ]; then
        rm -f "$MARKER"
        echo "✅ Memory pressure resolved: RAM ${MEM_PCT}%, swap ${SWAP_USED}MB. Stopping reminders."
    fi
    # Silent — no output
    exit 0
fi
