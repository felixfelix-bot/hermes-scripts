#!/usr/bin/env bash
# fleet-seed.sh <source-path> [entry-id]
# Replicate a cold path to the t440 + x280 cold stores over FIPS, verify the
# manifest sha256 on BOTH ends, and append an index entry to the private repo.
# Never deletes the source (MOVE is a separate, gated step).
set -eu
src="$1"
host="$(hostname)"
day="$(date -u +%Y%m%d)"
id="${2:-fs-${host}-${day}-$(date -u +%H%M%S)}"
tool="${FLEETSTORE_TOOL:-$HOME/repos/fleet-store-index/fleetstore.py}"
[ -f "$tool" ] || tool="$HOME/.hermes/scripts/fleetstore.py"
repo="${FLEETSTORE_REPO:-$HOME/repos/fleet-store-index}"
store="/data/fleet-store/cold/${id}"
inner="${store}${src}"
src_hash="$(python3 "$tool" manifest "$src")"
echo "entry=$id src=$src hash=$src_hash"

ok=1
for H in t440-store x280-store; do
  ssh -o BatchMode=yes "$H" "mkdir -p '$store'" || { echo "mkdir failed $H"; ok=0; continue; }
  if rsync -aHAX --sparse --numeric-ids --relative "$src" "$H:$store/"; then
    dh="$(ssh -o BatchMode=yes "$H" "python3 ~/.fleetstore.py manifest '$inner'")"
    echo "  $H manifest: $dh"
    [ "$dh" = "$src_hash" ] || { echo "  MISMATCH on $H"; ok=0; }
  else
    echo "  transfer FAILED $H"; ok=0
  fi
done
[ "$ok" = 1 ] || { echo "SEED FAILED for $id"; exit 1; }

python3 - "$repo/nodes/${host}.jsonl" "$id" "$host" "$src" "$store" "$src_hash" <<'PY'
import json, sys, os
frag, eid, host, src, store, h = sys.argv[1:7]
os.makedirs(os.path.dirname(frag), exist_ok=True)
entry = {"id": eid, "seq": 0, "origin_host": host, "class": "cold",
         "source": src, "store": store, "manifest_sha256": h, "state": "stored",
         "size_allocated_bytes": 0}
with open(frag, "a") as fh:
    fh.write(json.dumps(entry, sort_keys=True) + "\n")
print("indexed", eid)
PY
echo "SEEDED $id (source kept)"
