#!/bin/bash
# Pricing system health check — runs every 30 min via cron
DB="/home/c03rad0r/.hermes/bot/zai_usage.db"
WARN=0

# 1. Proxy alive?
if ! systemctl --user is-active zai-proxy &>/dev/null; then
    echo "CRITICAL: zai-proxy not running"
    exit 1
fi

# 2. Any routing errors in last 30 min?
RECENT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM routing_live_decisions WHERE ts > strftime('%s','now')-1800;" 2>/dev/null || echo "0")
if [ "$RECENT" -lt 1 ]; then
    echo "WARN: No routing decisions in last 30 min (low traffic?)"
    WARN=1
fi

# 3. Divergence rate (live vs shadow disagree)
DIVERGE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM routing_live_decisions WHERE agree=0 AND ts > strftime('%s','now')-3600;" 2>/dev/null || echo "0")
if [ "$DIVERGE" -gt 5 ]; then
    echo "WARN: $DIVERGE routing divergences in last hour"
    WARN=1
fi

# 4. Dead endpoint traffic (PPQ/OpenRouter should have 0 traffic)
DEAD=$(sqlite3 "$DB" "SELECT COUNT(*) FROM routing_live_decisions WHERE live_provider IN ('ppq','openrouter') AND ts > strftime('%s','now')-3600;" 2>/dev/null || echo "0")
if [ "$DEAD" -gt 0 ]; then
    echo "CRITICAL: $DEAD requests routed to dead endpoints (ppq/openrouter) in last hour"
    WARN=1
fi

# 5. 429 rate from upstream
ERR429=$(journalctl --user -u zai-proxy --since "30 min ago" --no-pager 2>/dev/null | grep -c "429" || echo "0")
if [ "$ERR429" -gt 20 ]; then
    echo "WARN: $ERR429 429 errors in last 30 min"
    WARN=1
fi

# 6. Check which kill switches are active
ACTIVE_FLAGS=$(grep -h "PRESSURE_ENABLED=true\|PER_MODEL=true" /home/c03rad0r/.config/systemd/user/zai-proxy.service.d/*.conf 2>/dev/null | wc -l)
echo "Active pricing flags: $ACTIVE_FLAGS"
echo "Recent decisions (30m): $RECENT"
echo "Divergences (1h): $DIVERGE"
echo "Dead-endpoint traffic (1h): $DEAD"
echo "429 errors (30m): $ERR429"

if [ "$WARN" -eq 0 ]; then
    echo "STATUS: HEALTHY"
else
    echo "STATUS: WARN — investigate"
fi
