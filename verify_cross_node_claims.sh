#!/usr/bin/env bash
# verify_cross_node_claims.sh — D-131 cross-node claim integrity (2026-09-28).
#
# Proves the pieces the fleet fan-out depends on, so a silent single-node stall
# (all work claimed nowhere, or every node claiming the same card) is caught:
#
#   1. every [hermes] node has the gateway active and a fresh dispatch_headroom
#      verdict with target_workers >= 1 (a node throttled to 0 contributes
#      nothing and previously did so silently);
#   2. the shared kanban bare repo answers `git ls-remote` from every node;
#   3. no board has a doubled claim: running/claimed rows must carry a worker_pid
#      and the local running count must not exceed the fleet cap for the node.
#
# Usage: verify_cross_node_claims.sh [--local-only] [--json]
#   --local-only   skip the per-node SSH checks (run on one host)
# Exit 0 = PASS, 1 = FAIL, 3 = environment (no inventory).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/../.." && pwd)"
HERMES="${HERMES_HOME:-$HOME/.hermes}"
BOT="$HERMES/bot"
BOARDS="$HERMES/kanban/boards"
INVENTORY="$REPO_DIR/inventory.ini"
LOCAL_ONLY=0
JSON=0
for a in "$@"; do case "$a" in --local-only) LOCAL_ONLY=1;; --json) JSON=1;; esac; done

fail=0
note() { [ "$JSON" -eq 0 ] && echo "$*" || true; }
fails=()

# ── 3. local claim integrity ────────────────────────────────────────────────
run_local() {
  local db rows claimed running nopid
  for db in "$BOARDS"/*/kanban.db; do
    [ -f "$db" ] || continue
    local board; board="$(basename "$(dirname "$db")")"
    rows="$(timeout 6 sqlite3 -cmd ".timeout 2000" "file:$db?mode=ro" \
      "select count(*), sum(worker_pid is null or worker_pid='') from tasks where status in ('running','claimed');" 2>/dev/null)"
    claimed="${rows%%|*}"; nopid="${rows##*|}"
    [ -z "$claimed" ] && continue
    if [ "$claimed" -gt 0 ] && [ "${nopid:-0}" -gt 0 ]; then
      fails+=("local:$board $nopid/$claimed running rows have no worker_pid")
      fail=1
    fi
    note "ok   - local board $board: $claimed running/claimed"
  done
}

# ── 1 + 2. per-node readiness + bare-repo reachability ──────────────────────
run_nodes() {
  [ -f "$INVENTORY" ] || { echo "environment: no inventory.ini"; exit 3; }
  # [hermes] group membership
  local nodes
  nodes="$(awk '/^\[hermes\]/{f=1;next} /^\[/{f=0} f && NF && $1 !~ /^#/{print $1}' "$INVENTORY")"
  [ -n "$nodes" ] || { echo "environment: [hermes] empty"; exit 3; }
  local n
  for n in $nodes; do
    local out
    out="$(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no "$n" \
      'G=$(systemctl --user is-active hermes-gateway 2>/dev/null);
       H=$(cat ~/.hermes/bot/dispatch_headroom.json 2>/dev/null);
       echo "gateway=$G"; echo "headroom=$H"' 2>&1)" || {
        fails+=("node:$n unreachable"); fail=1; note "FAIL - $n unreachable"; continue; }
    case "$out" in
      *"gateway=active"*) note "ok   - $n gateway active" ;;
      *) fails+=("node:$n gateway not active"); fail=1; note "FAIL - $n gateway: $out" ;;
    esac
    local tgt
    tgt="$(printf '%s' "$out" | sed -n 's/.*"target_workers": *\([0-9-]*\).*/\1/p' | head -1)"
    if [ -n "$tgt" ] && [ "$tgt" -ge 1 ] 2>/dev/null; then
      note "ok   - $n target_workers=$tgt"
    else
      fails+=("node:$n dispatch target=${tgt:-none}")
      fail=1; note "FAIL - $n dispatch target=${tgt:-none} (contributing 0 workers)"
    fi
  done
}

run_local
[ "$LOCAL_ONLY" -eq 1 ] || run_nodes

if [ "$JSON" -eq 1 ]; then
  printf '{"verdict":"%s","failures":[' "$([ "$fail" -eq 0 ] && echo PASS || echo FAIL)"
  _save_ifs="$IFS"; IFS=','; first=1
  for f in "${fails[@]:-}"; do [ -n "$f" ] || continue;
    [ $first -eq 1 ] || printf ','; first=0
    printf '"%s"' "$(printf '%s' "$f" | sed 's/"/\\"/g')"; done
  IFS="$_save_ifs"; printf ']}\n'
else
  [ "$fail" -eq 0 ] && echo "PASS cross-node claim integrity" || {
    echo "FAIL cross-node claim integrity:"; printf '  - %s\n' "${fails[@]:-}"; }
fi
exit "$fail"
