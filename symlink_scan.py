#!/usr/bin/env python3
"""symlink_scan.py — find home symlinks that resolve into the offload mount.

Phase P2. A symlink like `~/go -> /mnt/dq05-lexar/...` silently breaks anything
that depends on it when the mount drops. This scan lists them and compares
against a known/migrated allowlist. **Advisory by default** (per operator):
alerts only, until the known set is migrated; with `--enforce` and an empty
allowlist it fails (rc=1).

Usage: symlink_scan.py [--enforce] [--json]
"""
from __future__ import annotations

import json
import os
import sys

HOME = os.path.expanduser("~")
MOUNT = os.environ.get("OFFLOAD_MOUNT", "/mnt/dq05-lexar")
ALLOWLIST = os.environ.get("SYMLINK_ALLOWLIST",
                           os.path.join(HOME, ".hermes/bot/mount_symlinks.json"))


def scan(home: str = HOME, mount: str = MOUNT) -> list[dict]:
    hits = []
    try:
        names = os.listdir(home)
    except OSError:
        return hits
    for n in names:
        p = os.path.join(home, n)
        if os.path.islink(p):
            t = os.readlink(p)
            if t.startswith(mount):
                hits.append({"link": p, "target": t})
    return sorted(hits, key=lambda x: x["link"])


def load_allow(path: str = ALLOWLIST) -> set[str]:
    try:
        return set(json.load(open(path)).get("known", []))
    except Exception:
        return set()


def main(argv: list[str]) -> int:
    hits = scan()
    known = load_allow()
    new = [h for h in hits if h["link"] not in known]
    if "--json" in argv:
        print(json.dumps({"mount": MOUNT, "hits": hits, "known": sorted(known),
                          "new": new, "enforce_ready": not known}))
        return 0
    print(f"symlink-scan: {len(hits)} symlink(s) into {MOUNT}; "
          f"{len(new)} not in the migrated allowlist")
    for h in hits:
        print(f"  {h['link']} -> {h['target']}")
    if new:
        print("ADVISORY: migrate these off the mount (Phase P2); then prune the allowlist.")
    if "--enforce" in argv and not known and hits:
        print("ENFORCED: unmigrated symlinks present")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
