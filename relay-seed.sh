#!/usr/bin/env bash
# relay-seed.sh <dq05-source-abs-path> [entry-id]
# Seed a dq05 cold path into the t440+x280 cold store, relaying through THIS
# host (dq05 has no direct route to the laptops). Streams with tar (preserves
# hardlinks + sparse), verifies the manifest sha256 on BOTH ends, then indexes.
# Never deletes the source.
set -eu
src="$1"
host="dq05"
day="$(date -u +%Y%m%d)"
id="${2:-fs-${host}-${day}-$(date -u +%H%M%S)}"
tool="$HOME/.hermes/scripts/fleetstore.py"
repo="$HOME/repos/fleet-store-index"
parent="$(dirname "$src")"
name="$(basename "$src")"
src_hash="$(ssh -o BatchMode=yes dq05 "python3 ~/.fleetstore.py manifest '$src'")"
echo "entry=$id src=$src hash=$src_hash"

ok=1
for H in t440-store x280-store; do
  dest="/data/fleet-store/cold/${id}${parent}"
  if ! ssh -o BatchMode=yes "$H" "mkdir -p '$dest'"; then echo "  mkdir failed $H"; ok=0; continue; fi
  if ssh -o BatchMode=yes dq05 "tar -C '$parent' -cSf - '$name'" | ssh -o BatchMode=yes "$H" "tar -C '$dest' -xpSf -"; then
    dh="$(ssh -o BatchMode=yes "$H" "python3 ~/.fleetstore.py manifest '${dest}/${name}'")"
    echo "  $H manifest: $dh"
    [ "$dh" = "$src_hash" ] || { echo "  MISMATCH on $H"; ok=0; }
  else
    echo "  stream FAILED $H"; ok=0
  fi
done
[ "$ok" = 1 ] || { echo "SEED FAILED for $id"; exit 1; }

python3 - "$repo/nodes/${host}.jsonl" "$id" "$host" "$src" "/data/fleet-store/cold/${id}" "$src_hash" <<'PY'
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
