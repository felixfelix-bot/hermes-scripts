#!/usr/bin/env bash
# fix-worker-glm-baseurl.sh - pin worker-profile GLM calls to the node's router.
#
# WHY (2026-10-09 x280 incident): a worker profile .env that sets GLM_API_KEY
# without GLM_BASE_URL makes hermes register the credential with the DEFAULT
# z.ai base_url (https://api.z.ai). Offloaded workers then call z.ai DIRECTLY
# with a node-local key instead of the market router - usage bypasses the
# Kalman filters and quota accounting, and a stale direct key yields 401
# "token expired", killing every spawned worker ~5s in.
#
# ROUTER-LESS NODES: the consultant verified hermes-nvme has NO local 9099
# listener. Pinning those nodes to localhost would break ALL their inference,
# so this script verifies the router first and refuses unless --force is given.
#
# Idempotent. --dry-run previews. --force skips the router-reachability check.
set -euo pipefail
DRY=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
HH="${HERMES_HOME:-$HOME/.hermes}"
ROUTER_URL="${FLEET_ROUTER_URL:-http://127.0.0.1:9099}"

if [ "$FORCE" != 1 ]; then
  hp="${ROUTER_URL#http://}"; hp="${hp#https://}"; hp="${hp%%/*}"
  if ! timeout 3 bash -c "exec 3<>/dev/tcp/${hp%:*}/${hp##*:}" 2>/dev/null; then
    echo "WARN: no router reachable at $ROUTER_URL - refusing to pin worker GLM base_url"
    echo "      (router-less nodes must reach the federation router, not localhost)"
    echo "      override with --force only if you know this node should use localhost"
    exit 0
  fi
fi

n=0
for envf in "$HH"/profiles/worker-*/.env; do
  [ -f "$envf" ] || continue
  if grep -q '^GLM_API_KEY=' "$envf" && ! grep -q '^GLM_BASE_URL=' "$envf"; then
    if [ "$DRY" = 1 ]; then echo "would fix: $envf"; else
      printf 'GLM_BASE_URL=%s\n' "$ROUTER_URL" >> "$envf" && echo "fixed: $envf"; fi
    n=$((n+1))
  fi
done
[ "$n" = 0 ] && echo "ok: all worker profiles already pinned (or no GLM keys)"
exit 0
