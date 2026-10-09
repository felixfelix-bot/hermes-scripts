#!/usr/bin/env bash
# install-offload-optin.sh - merge config/offload_boards.json into the live
# fleet offload map (~/.hermes/bot/fleet_map/private_offload_boards.json).
#
# Config-as-code: the tracked config is the source of truth for extra boards we
# opt into the PRIVATE offload ledger. Idempotent; atomic; keeps a backup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${OFFLOAD_CFG:-$HERE/config/offload_boards.json}"
MAP="${FLEET_MAP:-$HOME/.hermes/bot/fleet_map/private_offload_boards.json}"
[ -f "$CFG" ] || { echo "offload-optin: missing config $CFG" >&2; exit 1; }
mkdir -p "$(dirname "$MAP")"
python3 - "$CFG" "$MAP" <<'PY'
import json, os, shutil, sys, tempfile
cfg_p, map_p = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_p)).get("private_offload", {}) or {}
live = {}
if os.path.exists(map_p):
    try: live = json.load(open(map_p)) or {}
    except Exception: live = {}
if not isinstance(live, dict): live = {}
added, updated = [], []
for board, meta in cfg.items():
    entry = {k: v for k, v in meta.items() if k != "note"}
    if board not in live:
        added.append(board)
    elif live.get(board, {}).get("repo") != entry.get("repo"):
        updated.append(board)
    live[board] = {**live.get(board, {}), **entry}
if not (added or updated):
    print("offload-optin: already current (no change)")
    sys.exit(0)
if os.path.exists(map_p):
    shutil.copy2(map_p, map_p + ".bak-offload-optin")
d = os.path.dirname(map_p) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".offload-", suffix=".json")
with os.fdopen(fd, "w") as fh:
    json.dump(live, fh, indent=1, sort_keys=True)
    fh.write("\n")
os.replace(tmp, map_p)
print("offload-optin: added=%s updated=%s -> %s" % (added, updated, map_p))
PY
