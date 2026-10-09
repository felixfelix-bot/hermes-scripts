#!/usr/bin/env bash
# install-offload-optin.sh - opt a board into PRIVATE fleet offload, durably.
#
# WHY (2026-10-09): the offload opt-in map
# (~/.hermes/bot/fleet_map/private_offload_boards.json) is DERIVED - the
# classifier/materializer rebuilds it from ~/.hermes/bot/board_repos.json on
# every cycle. Writing the derived map directly LOOKS like it works, then the
# entry silently disappears on the next classify/materialize pass and the
# board's cards stop being advertised to peers. So this installer writes the
# UPSTREAM mapping (board -> repo) and lets the derivation carry it forward.
#
# Idempotent. --dry-run prints changes. HERMES_BOT overrides the bot dir.
set -euo pipefail
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT="${HERMES_BOT:-$HOME/.hermes/bot}"
CFG="$HERE/config/offload_boards.json"
[ -f "$CFG" ] || { echo "ERROR: missing $CFG" >&2; exit 1; }

python3 - "$CFG" "$BOT" "$DRY" <<'PY'
import json, os, sys, urllib.parse
cfg_p, bot, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
cfg = json.load(open(cfg_p))["private_offload"]
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
# 1) upstream mapping: the thing that must survive
repos_p = os.path.join(bot, "board_repos.json")
repos = load(repos_p)
added = []
for board, meta in cfg.items():
    slug = urllib.parse.urlparse(meta["repo"]).path.rstrip("/").removesuffix(".git").split("/")[-1]
    if repos.get(board) != slug:
        repos[board] = slug
        added.append(board)
if dry:
    print(f"DRY: would add to board_repos.json: {added}")
else:
    if added:
        json.dump(repos, open(repos_p + ".tmp", "w"), indent=1)
        os.replace(repos_p + ".tmp", repos_p)
    print(f"board_repos.json: added={added} (now {len(repos)} boards)")
# 2) derived opt-in: write now so it takes effect before the next derive cycle
fm = os.path.join(bot, "fleet_map")
os.makedirs(fm, exist_ok=True)
opt_p = os.path.join(fm, "private_offload_boards.json")
opt = load(opt_p)
missed = []
for board, meta in cfg.items():
    if board not in opt:
        opt[board] = {"repo": meta["repo"], "private": True}
        missed.append(board)
if not dry and missed:
    json.dump(opt, open(opt_p + ".tmp", "w"), indent=1)
    os.replace(opt_p + ".tmp", opt_p)
print(f"private_offload_boards.json: added={missed} (now {len(opt)})")
PY
echo "verify: python3 -c \"import sys;sys.path.insert(0,'$HERE');import fleet_scheduler as f;print([(b, f._advertise_transport(b, f._load_map('public_boards.json'), f._load_map('private_offload_boards.json'), f._load_map('local_only_boards.json'))) for b in $(python3 -c "import json;print(list(json.load(open('$CFG'))['private_offload']))")])\""
