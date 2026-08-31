#!/usr/bin/env bash
# agentmemory_activate — start agentmemory server + wire into Hermes.
# Run this manually when you want to enable persistent memory.
set -euo pipefail

echo "=== Starting agentmemory server ==="
echo "  Ports: 3111=REST, 3112=streams, 3113=viewer, 49134=engine"
agentmemory &
AM_PID=$!
echo "  PID: $AM_PID"

echo "=== Waiting for server to be reachable ==="
for i in $(seq 1 15); do
  if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1; then
    echo "  Server is up."
    break
  fi
  sleep 1
done

if ! curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1; then
  echo "ERROR: agentmemory server did not start."
  exit 1
fi

echo "=== Wiring MCP into Hermes ==="
agentmemory connect hermes

echo "=== Installing native skills ==="
npx skills add rohitg00/agentmemory -y 2>/dev/null || echo "  (skills install skipped)"

echo ""
echo "=== DONE ==="
echo "Server: http://localhost:3111"
echo "Viewer: http://localhost:3113"
echo "Restart Hermes gateway to pick up MCP tools."
echo ""
echo "To verify: hermes memory status"
