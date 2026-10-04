#!/usr/bin/env python3
"""fleetstore.py — fleet cold-store index: Phase 1 (format, verifier, CLI).

Implements the safety invariants of docs/PLAN-fleet-cold-storage-index.md (v2):

  I1  never delete before a manifest-verified copy exists elsewhere
  I2  the index records WHERE, integrity is proven by sha256
  I5  MOVE != REPLICATE
  I6  fail closed: any unreadable/special/skipped path => refuse ``state: stored``

Transfer command is pinned and must not be changed casually:
    rsync -aHAX --sparse --numeric-ids --relative <src> <dest>

Manifests must be computed with root privileges on BOTH ends (root-owned paths
are silently skipped by non-root rsync; see plan section 4).

This module is intentionally dependency-free (stdlib only) so it can run on any
fleet node with python3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RSYNC_ARGS = ["rsync", "-aHAX", "--sparse", "--numeric-ids", "--relative"]

# Secret-bearing basenames/prefixes: a tree containing any of these is HOT and
# must never be MOVE'd (plan section 7). Matching is fail-closed.
DENY_SUBSTR = (
    "nsec", "kdbx", ".key", "key4.db", "cert9.db", "cookies.sqlite",
    "logins.json", "auth.json", "hosts.yml", "credentials", ".pem", ".p12",
    "id_rsa", "id_ed25519", "token", "secret",
)
DENY_PREFIXES = (".ssh/", ".hermes/keys/", ".local/share/keyrings/", ".config/nak/")


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _is_denylisted(relpath: str) -> bool:
    base = os.path.basename(relpath).lower()
    if any(s in base for s in DENY_SUBSTR):
        return True
    return any(prefix in relpath for prefix in DENY_PREFIXES)


def build_manifest(root: Path) -> dict:
    """Return {entries, unreadable, special, denied, total_allocated_bytes}.

    Every path is recorded; anything that cannot be read/hashed is recorded in
    ``unreadable`` (never omitted). A non-empty unreadable/special/denied list
    makes the entry unsafe to store (fail closed, I6).
    """
    root = root.resolve()
    entries: list[dict] = []
    unreadable: list[str] = []
    special: list[str] = []
    denied: list[str] = []
    total = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in sorted(dirnames + filenames):
            full = Path(dirpath) / name
            rel = str(full.relative_to(root))
            if _is_denylisted(rel):
                denied.append(rel)
            try:
                st = os.lstat(full)
            except OSError as exc:
                unreadable.append(f"{rel}: lstat: {exc}")
                continue
            mode = st.st_mode
            rec: dict = {
                "relpath": rel,
                "mode": stat.S_IMODE(mode),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "size": st.st_size,
                "allocated_bytes": st.st_blocks * 512,
            }
            if stat.S_ISLNK(mode):
                rec["type"] = "symlink"
                try:
                    rec["target"] = os.readlink(full)
                except OSError as exc:
                    unreadable.append(f"{rel}: readlink: {exc}")
            elif stat.S_ISDIR(mode):
                rec["type"] = "dir"
            elif stat.S_ISREG(mode):
                rec["type"] = "file"
                try:
                    rec["sha256"] = sha256_file(full)
                except OSError as exc:
                    unreadable.append(f"{rel}: read: {exc}")
            else:
                rec["type"] = "special"
                special.append(rel)
            entries.append(rec)
            total += rec["allocated_bytes"]

    return {
        "entries": entries,
        "unreadable": unreadable,
        "special": special,
        "denied": denied,
        "total_allocated_bytes": total,
    }


def manifest_sha256(manifest: dict) -> str:
    payload = json.dumps(
        {k: manifest[k] for k in ("entries", "unreadable", "special", "denied")},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def manifest_is_storable(manifest: dict) -> tuple[bool, str]:
    if manifest["denied"]:
        return False, f"denylisted paths present: {len(manifest['denied'])}"
    if manifest["unreadable"]:
        return False, f"unreadable paths present: {len(manifest['unreadable'])}"
    if manifest["special"]:
        return False, f"special files present: {len(manifest['special'])}"
    if not manifest["entries"]:
        return False, "empty or missing source (0 entries)"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Transfer
# --------------------------------------------------------------------------- #
def transfer(src: Path, dest_root: Path, *, dry_run: bool = False) -> int:
    """Copy ``src`` under ``dest_root`` preserving its full path (`--relative`)."""
    dest_root.mkdir(parents=True, exist_ok=True)
    # Trailing /. anchors --relative so the absolute source path is reproduced.
    src_arg = str(src) if str(src).endswith("/") else str(src)
    cmd = RSYNC_ARGS + (["--dry-run"] if dry_run else []) + [src_arg, str(dest_root) + "/"]
    return subprocess.run(cmd, check=False).returncode


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_id(host: str, seq: int, day: str | None = None) -> str:
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"fs-{host}-{day}-{seq:04d}"


def index_dir(repo: Path) -> Path:
    return repo / "nodes"


def append_fragment(repo: Path, host: str, entry: dict) -> Path:
    frag = index_dir(repo) / f"{host}.jsonl"
    frag.parent.mkdir(parents=True, exist_ok=True)
    with open(frag, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return frag


def read_index(repo: Path) -> list[dict]:
    out: list[dict] = []
    for frag in sorted(index_dir(repo).glob("*.jsonl")):
        for line in frag.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_put(args) -> int:
    host = args.host or socket.gethostname()
    src = Path(args.source).resolve()
    dest_root = Path(args.store).resolve()
    if os.geteuid() != 0:
        print("ERROR: put must run as root (manifests must be root on both ends)", file=sys.stderr)
        return 2

    src_manifest = build_manifest(src)
    ok, why = manifest_is_storable(src_manifest)
    if not ok:
        print(f"REFUSE (fail closed): {why}", file=sys.stderr)
        return 3

    seq = sum(1 for e in read_index(Path(args.repo)) if e.get("origin_host") == host) + 1
    eid = entry_id(host, seq)
    dest = dest_root / eid
    print(f"transfer {src} -> {dest}")
    rc = transfer(src, dest)
    if rc != 0:
        print(f"ERROR: rsync failed rc={rc}", file=sys.stderr)
        return rc

    dest_manifest = build_manifest(dest / str(src).lstrip("/"))
    src_hash, dest_hash = manifest_sha256(src_manifest), manifest_sha256(dest_manifest)
    entry = {
        "id": eid,
        "seq": seq,
        "origin_host": host,
        "class": "cold",
        "created_at": utcnow(),
        "source": str(src),
        "store": str(dest),
        "manifest_sha256": src_hash,
        "dest_manifest_sha256": dest_hash,
        "files": len(src_manifest["entries"]),
        "size_allocated_bytes": src_manifest["total_allocated_bytes"],
        "state": "stored" if src_hash == dest_hash else "mismatch",
    }
    if src_hash != dest_hash:
        print("REFUSE: manifest mismatch between source and destination", file=sys.stderr)
        return 4
    append_fragment(Path(args.repo), host, entry)
    print(f"stored {eid} ({entry['files']} paths, {entry['size_allocated_bytes']} bytes)")
    print("NOTE: source NOT deleted (MOVE requires push + replica; see plan I1/I5).")
    return 0


def cmd_verify(args) -> int:
    for e in read_index(Path(args.repo)):
        if args.id and e["id"] != args.id:
            continue
        stored = Path(e["store"])
        m = build_manifest(stored / str(Path(e["source"])).lstrip("/"))
        h = manifest_sha256(m)
        status = "OK" if h == e["manifest_sha256"] else "DRIFT"
        print(f"{e['id']}: {status} ({len(m['entries'])} paths)")
    return 0


def cmd_index(args) -> int:
    entries = read_index(Path(args.repo))
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
    else:
        for e in entries:
            print(f"{e['id']:28} {e['origin_host']:10} {e['state']:8} {e['store']}")
    return 0


def cmd_manifest(args) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: source does not exist: {path}", file=sys.stderr)
        return 2
    m = build_manifest(path.resolve())
    h = manifest_sha256(m)
    if args.json:
        print(json.dumps({**m, "manifest_sha256": h}, indent=2, sort_keys=True))
    else:
        print(h)
    return 0


def cmd_get(args) -> int:
    for e in read_index(Path(args.repo)):
        if e["id"] != args.id:
            continue
        src = e["store"]
        print(f"restore {e['id']} from {src} -> {args.dest}")
        return transfer(Path(src), Path(args.dest).resolve())
    print(f"no entry {args.id} (owner may be offline — never 'not found')", file=sys.stderr)
    return 5


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="fleet cold-store index (Phase 1)")
    ap.add_argument("--repo", default=str(Path.home() / "repos" / "fleet-store-index"),
                    help="index repo working copy")
    ap.add_argument("--store", default=str(Path.home() / "fleet-store" / "cold"),
                    help="physical store root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("put")
    p.add_argument("source")
    p.add_argument("--host", default=None)
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("verify")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("manifest")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("get")
    p.add_argument("id")
    p.add_argument("dest")
    p.set_defaults(func=cmd_get)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
