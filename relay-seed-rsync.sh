#!/usr/bin/env bash
# relay-seed-rsync.sh <dq05-source-abs-path> [entry-id]
# Robust rsync-based seed (relays through this host; dq05 can't reach the
# laptops). Reads the source ONCE into a local staging copy, then rsyncs to
# t440 + x280. Avoids the tar-stream truncation the tar/tee broadcast hit and
# handles read-only dirs natively. Verifies a metadata signature on both ends.
# Never deletes the source.
set -u
src="$1"
host="dq05"
day="$(date -u +%Y%m%d)"
id="${2:-fs-${host}-${day}-$(date -u +%H%M%S)}"
repo="$HOME/repos/fleet-store-index"
name="$(basename "$src")"
parent="$(dirname "$src")"          # e.g. /home/c03rad0r/backups/cobradorwave
stage="$(mktemp -d "$HOME/.cache/fleet-stage.XXXXXX" 2>/dev/null || mktemp -d)"
trap 'rm -rf "$stage"' EXIT

ssh -o BatchMode=yes dq05 "test -e '$src'" || { echo "REFUSE: source missing: $src"; exit 2; }

# 1) pull dq05 -> local stage (single read of dq05), preserving the path after /.
rsync -aHAX --sparse --numeric-ids --relative "dq05:${parent}/./${name}" "$stage/" \
  || { echo "pull from dq05 failed"; exit 1; }
staged="$stage/$name"

sig_of() { ( cd "$1" && find "./$name" -printf '%P|%y|%s|%m\n' 2>/dev/null | LC_ALL=C sort | sha256sum | cut -d' ' -f1 ); }
src_sig="$(sig_of "$stage")"
echo "entry=$id src=$src sig=$src_sig"

destroot="/data/fleet-store/cold/${id}${parent}"
ok=1
for H in t440-store x280-store; do
  if ! ssh -o BatchMode=yes "$H" "mkdir -p '$destroot/$name'"; then echo "  mkdir failed $H"; ok=0; continue; fi
  if rsync -aHAX --sparse --numeric-ids "$staged/" "$H:$destroot/$name/"; then
    dsig="$(ssh -o BatchMode=yes "$H" "cd '$destroot' && find './$name' -printf '%P|%y|%s|%m\n' 2>/dev/null | LC_ALL=C sort | sha256sum | cut -d' ' -f1")"
    echo "  $H sig: $dsig"
    [ "$dsig" = "$src_sig" ] || { echo "  MISMATCH on $H"; ok=0; }
  else
    echo "  push FAILED $H"; ok=0
  fi
done
[ "$ok" = 1 ] || { echo "SEED FAILED for $id"; exit 1; }

python3 - "$repo/nodes/${host}.jsonl" "$id" "$host" "$src" "/data/fleet-store/cold/${id}" "$src_sig" <<'PY'
import json, sys, os
frag, eid, host, src, store, h = sys.argv[1:7]
os.makedirs(os.path.dirname(frag), exist_ok=True)
entry = {"id": eid, "seq": 0, "origin_host": host, "class": "cold",
         "source": src, "store": store, "manifest_sha256": h,
         "verify": "metadata_signature+rsync", "state": "stored",
         "size_allocated_bytes": 0}
with open(frag, "a") as fh:
    fh.write(json.dumps(entry, sort_keys=True) + "\n")
print("indexed", eid)
PY
echo "SEEDED $id (rsync; source kept)"
