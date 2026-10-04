#!/usr/bin/env bash
# router_restore.sh — one-command recovery of the live router from LKG (D-133).
#
# Restores the most recent LKG snapshot that contains the router files
# (~/.hermes/bot/zai_proxy.py + flat_router.py), then restarts the proxy and
# verifies /health. Usage:
#   router_restore.sh [snapshot-id] [--no-restart] [--dry-run]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LKG="$HERE/fleet_lkg.py"
[ -f "$LKG" ] || LKG="$HOME/.hermes/scripts/fleet_lkg.py"
[ -f "$LKG" ] || { echo "fleet_lkg.py not found"; exit 2; }

ID=""; RESTART=1; DRY=0
for a in "$@"; do
  case "$a" in
    --no-restart) RESTART=0 ;;
    --dry-run) DRY=1 ;;
    -*) echo "unknown flag $a" >&2; exit 2 ;;
    *) ID="$a" ;;
  esac
done

if [ -z "$ID" ]; then
  ID=$(python3 - "$LKG" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("fleet_lkg", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
idx = m._load_index()
c = [s for s in idx.get("snapshots", [])
     if any(k.endswith("bot/zai_proxy.py") for k in s.get("files", {}))]
c.sort(key=lambda s: s.get("ts", 0), reverse=True)
print(c[0]["id"] if c else "")
PY
)
fi
[ -n "$ID" ] || { echo "no LKG snapshot contains the router"; exit 2; }
echo "restoring router from LKG snapshot: $ID"
if [ "$DRY" = 1 ]; then
  python3 "$LKG" restore "$ID" --dry-run
  exit 0
fi
python3 "$LKG" restore "$ID" --yes || exit 1
if [ "$RESTART" = 1 ]; then
  systemctl --user restart zai-proxy.service || true
  sleep 4
  echo "active=$(systemctl --user is-active zai-proxy.service)"
  echo "health=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9099/health)"
fi
