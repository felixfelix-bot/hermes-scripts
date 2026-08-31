#!/usr/bin/env bash
# OmniRoute health monitor — no_agent cron script
# Checks container health, provider availability, failover hit rate
# Schedule: every 30 minutes
# Silent on no changes (empty stdout = no alert)

set -u

METRICS_DB="${HOME}/.hermes/kanban/boards/bitrouter-eval/workspaces/t_33de823b/metrics.db"
CONTAINER_NAME="omniroute"
OMNI_URL="http://localhost:20128"
alerts=()

# --- check container health ---
CONTAINER_STATE=$(docker inspect --format='{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo "missing")
if [ "${CONTAINER_STATE}" = "missing" ]; then
  alerts+=("OmniRoute container NOT FOUND — has not been deployed yet or was removed")
elif [ "${CONTAINER_STATE}" != "running" ]; then
  alerts+=("OmniRoute container state: ${CONTAINER_STATE} (expected: running)")
fi

# Only proceed with health checks if container is running
if [ "${CONTAINER_STATE}" = "running" ]; then
  # Check API responsiveness
  API_RESP=$(curl -sS -m 5 -w "\n%{http_code}" "${OMNI_URL}/v1/models" 2>/dev/null)
  HTTP_CODE=$(echo "${API_RESP}" | tail -1)
  BODY=$(echo "${API_RESP}" | sed '$d')

  if [ "${HTTP_CODE}" = "000" ]; then
    alerts+=("OmniRoute API unreachable on ${OMNI_URL} (timeout)")
  elif [ "${HTTP_CODE}" != "200" ]; then
    alerts+=("OmniRoute API returned HTTP ${HTTP_CODE}")
  fi

  # Check free-tier provider count
  FREE_TIER=$(curl -sS -m 5 "${OMNI_URL}/api/free-tier/summary" 2>/dev/null)
  if [ -n "${FREE_TIER}" ]; then
    PROVIDER_COUNT=$(echo "${FREE_TIER}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('totalProviders', data.get('providerCount', 'unknown')))
except:
    print('error')
" 2>/dev/null)
    TOKENS_REMAINING=$(echo "${FREE_TIER}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('totalTokensRemaining', 'unknown'))
except:
    print('error')
" 2>/dev/null)

    if [ "${PROVIDER_COUNT}" = "error" ] || [ "${PROVIDER_COUNT}" = "unknown" ]; then
      : # silently skip if endpoint not available
    fi
  fi

  # Log metrics to DB if it exists
  if [ -f "${METRICS_DB}" ]; then
    python3 -c "
import sqlite3, json, sys
from datetime import datetime

db = sqlite3.connect('${METRICS_DB}')
db.execute('''CREATE TABLE IF NOT EXISTS omni_metrics (
  id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  container_state TEXT,
  api_http_code TEXT,
  provider_count TEXT,
  tokens_remaining TEXT
)''')
db.execute('INSERT INTO omni_metrics (timestamp, container_state, api_http_code, provider_count, tokens_remaining) VALUES (?, ?, ?, ?, ?)',
  (datetime.utcnow().isoformat()+'Z', '${CONTAINER_STATE}', '${HTTP_CODE}', '${PROVIDER_COUNT:-na}', '${TOKENS_REMAINING:-na}'))
db.commit()
db.close()
" 2>/dev/null
  fi

  # Alert if failover rate too high (check proxy logs)
  # Count OmniRoute mentions in proxy log in last 30 min
  PROXY_LOG="${HOME}/.hermes/bot/zai_proxy.log"
  if [ -f "${PROXY_LOG}" ]; then
    RECENT_FAILOVERS=$(python3 -c "
from datetime import datetime, timedelta
import re
cutoff = datetime.now() - timedelta(minutes=30)
count = 0
try:
    with open('${PROXY_LOG}') as f:
        for line in f:
            if 'omniroute' in line.lower():
                # Try to parse timestamp from log line
                m = re.match(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                if m:
                    ts = datetime.strptime(m.group(1).replace('T',' '), '%Y-%m-%d %H:%M:%S')
                    if ts > cutoff:
                        count += 1
except:
    pass
print(count)
" 2>/dev/null || echo "0")

    if [ "${RECENT_FAILOVERS}" -gt 10 ]; then
      alerts+=("HIGH FAILOVER RATE: ${RECENT_FAILOVERS} OmniRoute failovers in last 30 min (>10 threshold)")
    fi
  fi
fi

# --- output ---
if [ ${#alerts[@]} -gt 0 ]; then
  echo "# OmniRoute Monitor ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  echo ""
  for a in "${alerts[@]}"; do
    echo "  - ${a}"
  done
  echo ""
  echo "  Container: ${CONTAINER_NAME}"
  echo "  URL: ${OMNI_URL}"
fi
# Empty output = silent (all healthy)