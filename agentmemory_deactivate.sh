#!/usr/bin/env bash
# agentmemory_deactivate — stop server, disconnect from Hermes.
set -euo pipefail

echo "=== Stopping agentmemory server ==="
pkill -f "agentmemory" 2>/dev/null || echo "  (not running)"

echo "=== Removing MCP config from Hermes ==="
echo "  Remove the agentmemory entry from mcp_servers in ~/.hermes/config.yaml"
echo "  Then restart Hermes gateway."

echo "=== DONE ==="
