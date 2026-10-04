#!/usr/bin/env bash
# kanban-pump.sh — TEMPORARY dispatcher pump (2026-10-02). Remove after the
# cross-node claim-guard fix lands.
#
# WHY: _cross_node_claim_allows raises TypeError(board=None) on every dispatch
# tick of the GATEWAY process (its env carries KANBAN_SPAWN_CLAIM_CMD), so cards
# are claimed and never spawned. Running dispatch with the var unset for that
# process bypasses only this tick and spawns normally (verified 2026-10-02:
# `Spawned: 2` + live worker pids).
set -uo pipefail

BOARD=hermes-orchestration
H=/home/c03rad0r/.local/bin/hermes
DB=$HOME/.hermes/kanban/boards/$BOARD/kanban.db
STATE=/tmp/kanban-pump.count
N=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo "$N" > "$STATE"

OUT=$(env -u KANBAN_SPAWN_CLAIM_CMD timeout 240 "$H" kanban --board "$BOARD" dispatch --max 2 2>&1 \
      | grep -E "Spawned|Deferred|Auto-blocked|Timed out|Stale" | tr '\n' ' ')
ROWS=$(sqlite3 "$DB" "select id||'='||status||'/'||coalesce(worker_pid,'-') from tasks where id in ('t_885f20ec','t_d97a2be7','t_d79903b4','t_e8b40893');" | tr '\n' ' ')
OPEN=$(sqlite3 "$DB" "select count(*) from tasks where status in ('ready','running');")
LANE=$(curl -s -m 20 http://localhost:9099/v1/chat/completions -H 'content-type: application/json' \
        -d '{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"ok"}],"max_tokens":2}' \
        | head -c 55)

if [ "$N" -ge 14 ]; then
  echo "kanban-pump fire $N (LAST): $OUT| $ROWS| open=$OPEN| lane: $LANE"
  exit 0
fi
echo "kanban-pump fire $N: $OUT| $ROWS| open=$OPEN| lane: $LANE"
exit 0
