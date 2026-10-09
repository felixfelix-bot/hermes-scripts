#!/usr/bin/env bash
# fix-worker-glm-baseurl.sh - pin worker-profile GLM calls to the node's local router.
#
# WHY (2026-10-09 x280 incident): a worker profile .env that sets GLM_API_KEY
# without GLM_BASE_URL makes hermes register the credential with the DEFAULT
# z.ai base_url (https://api.z.ai). Offloaded fleet workers then call z.ai
# DIRECTLY with a node-local key instead of the node's market router on
# 127.0.0.1:9099 - so usage bypasses the Kalman filters, quota accounting sees
# nothing, and a stale direct key produces 401 "token expired" and kills every
# spawned worker ~5s in. The federation rule is: ALL inference flows through
# the node's local router. This script enforces it for every worker profile.
#
# Idempotent. --dry-run prints what would change. HERMES_HOME overrides.
set -euo pipefail
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
HH="${HERMES_HOME:-$HOME/.hermes}"
n=0
for envf in "$HH"/profiles/worker-*/.env; do
  [ -f "$envf" ] || continue
  if grep -q '^GLM_API_KEY=' "$envf" && ! grep -q '^GLM_BASE_URL=' "$envf"; then
    if [ "$DRY" = 1 ]; then echo "would fix: $envf"; else
      printf 'GLM_BASE_URL=http://127.0.0.1:9099\n' >> "$envf" && echo "fixed: $envf"; fi
    n=$((n+1))
  fi
done
[ "$n" = 0 ] && echo "ok: all worker profiles already pinned (or no GLM keys)"
exit 0
