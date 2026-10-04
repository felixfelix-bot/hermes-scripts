#!/bin/bash
# deploy_fleet_dashboard.sh — build + deploy the Hermes fleet dashboard as an nsite.
# Mirrors kalman-dashboard-deploy.sh: raw nsec (nsyte), Blossom + relays.
# Requires FLEET_DASHBOARD_NSEC in the env (or scripts/.env). Silent on success.
set -euo pipefail
export PATH="$HOME/.deno/bin:$HOME/.local/bin:$PATH"

NSITE_DIR="${FLEET_DASHBOARD_DIR:-$HOME/nsites/fleet-dashboard}"
BUILDER="$HOME/.hermes/scripts/build_fleet_dashboard.py"

python3 "$BUILDER" --out "$NSITE_DIR/index.html"

NSEC="${FLEET_DASHBOARD_NSEC:-}"
if [ -z "$NSEC" ] && [ -f "$(dirname "$0")/.env" ]; then
    # shellcheck disable=SC1091
    . "$(dirname "$0")/.env"
    NSEC="${FLEET_DASHBOARD_NSEC:-}"
fi
if [ -z "$NSEC" ]; then
    echo "built $NSITE_DIR/index.html (no FLEET_DASHBOARD_NSEC; skipped deploy)" >&2
    exit 0
fi

cd "$NSITE_DIR"
timeout 120 nsyte deploy . \
    --sec "$NSEC" \
    --relays wss://nos.lol,wss://relay.primal.net \
    --servers "https://blossom.primal.net,https://cdn.hzrd149.com,https://cdn.sovbit.host,https://nostr.download" \
    --non-interactive --skip-secrets-scan --force
