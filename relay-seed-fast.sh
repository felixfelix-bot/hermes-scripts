#!/usr/bin/env bash
# relay-seed-fast.sh <dq05-source-abs-path> [entry-id]
# FAST single-read seed path (relaying through this host):
#   * reads the source ONCE and broadcasts the tar stream to BOTH storage nodes
#     via a tee over FIFOs (dq05's degraded disk is the bottleneck, so one read
#     is ~2x faster than one-read-per-destination), and
#   * verifies a metadata signature (path|type|size|mode, sorted, sha256) on BOTH
#     ends. Byte integrity in transit is protected end-to-end by SSH (MAC) + tar
#     header checksums.
# Use relay-seed.sh (both-end full content sha256) when a content-level guarantee
# is required. Never deletes the source.
set -u
src="$1"
host="dq05"
day="$(date -u +%Y%m%d)"
id="${2:-fs-${host}-${day}-$(date -u +%H%M%S)}"
repo="$HOME/repos/fleet-store-index"
parent="$(dirname "$src")"
name="$(basename "$src")"
destroot="/data/fleet-store/cold/${id}${parent}"

sig_cmd() { ssh -o BatchMode=yes "$1" "cd $2 && find './$name' -printf '%P|%y|%s|%m\n' 2>/dev/null | LC_ALL=C sort | sha256sum | cut -d' ' -f1"; }

if ! ssh -o BatchMode=yes dq05 "test -e '$src'"; then echo "REFUSE: source does not exist: $src"; exit 2; fi
src_sig="$(sig_cmd dq05 "'$parent'")"
echo "entry=$id src=$src sig=$src_sig"

ssh -o BatchMode=yes t440-store "mkdir -p '$destroot'" || { echo "mkdir t440 failed"; exit 1; }
ssh -o BatchMode=yes x280-store "mkdir -p '$destroot'" || { echo "mkdir x280 failed"; exit 1; }

# NB: do NOT trust $TMPDIR here (systemd environment.d does not expand %h);
# use an explicit temp dir under HOME.
tmpbase="$HOME/.cache/hermes/tmp"
mkdir -p "$tmpbase" 2>/dev/null
tmp="$(mktemp -d "$tmpbase/fsi.XXXXXX" 2>/dev/null || mktemp -d)"
mkfifo "$tmp/t440" "$tmp/x280"
ssh -o BatchMode=yes t440-store "tar -C '$destroot' -xpSf - --delay-directory-restore" <"$tmp/t440" & p1=$!
ssh -o BatchMode=yes x280-store "tar -C '$destroot' -xpSf - --delay-directory-restore" <"$tmp/x280" & p2=$!
ssh -o BatchMode=yes dq05 "tar -C '$parent' -cSf - './$name'" | tee "$tmp/t440" "$tmp/x280" >/dev/null & p0=$!
wait "$p0"; r0=$?
wait "$p1"; r1=$?
wait "$p2"; r2=$?
rm -rf "$tmp"

ok=1
# tar rc 0 = ok; 1 = "some files differ" (benign warnings, e.g. mtime as
# non-root); 2 = fatal. Accept <=1 and rely on the signature comparison below.
[ "$r0" -le 1 ] || { echo "source tar failed rc=$r0"; ok=0; }
for pair in "t440:$r1" "x280:$r2"; do
  H="${pair%%:*}"; rc="${pair##*:}"
  [ "$rc" -le 1 ] || { echo "  extract FAILED on $H rc=$rc"; ok=0; }
done
if [ "$ok" = 1 ]; then
  for H in t440-store x280-store; do
    dsig="$(sig_cmd "$H" "'$destroot'")"
    echo "  $H sig: $dsig"
    [ "$dsig" = "$src_sig" ] || { echo "  MISMATCH on $H"; ok=0; }
  done
fi
[ "$ok" = 1 ] || { echo "SEED FAILED for $id"; exit 1; }

python3 - "$repo/nodes/${host}.jsonl" "$id" "$host" "$src" "/data/fleet-store/cold/${id}" "$src_sig" <<'PY'
import json, sys, os
frag, eid, host, src, store, h = sys.argv[1:7]
os.makedirs(os.path.dirname(frag), exist_ok=True)
entry = {"id": eid, "seq": 0, "origin_host": host, "class": "cold",
         "source": src, "store": store, "manifest_sha256": h,
         "verify": "metadata_signature+ssh", "state": "stored",
         "size_allocated_bytes": 0}
with open(frag, "a") as fh:
    fh.write(json.dumps(entry, sort_keys=True) + "\n")
print("indexed", eid)
PY
echo "SEEDED $id (fast; source kept)"
