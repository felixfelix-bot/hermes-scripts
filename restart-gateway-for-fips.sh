#!/bin/bash
# Gateway restart script for ESP-NOW FIPS mesh configuration
# This script restarts the Hermes gateway to activate the kanban dispatcher

echo "Restarting Hermes gateway to activate kanban dispatcher..."
hermes gateway restart

if [ $? -eq 0 ]; then
    echo "✅ Gateway restarted successfully at $(date)"
    echo "🚀 Kanban dispatcher now active - ESP-NOW FIPS mesh tasks will be picked up"
else
    echo "❌ Gateway restart failed at $(date)"
fi