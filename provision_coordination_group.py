#!/usr/bin/env python3
"""provision_coordination_group.py — idempotently ensure the private Buzz
coordination group used by the responder election (D-129).

Creates the OrangeSync NIP-29 group `fleet-responder-ops` (if missing), adds every
fleet node identity as a member, and records the generated group id so new nodes
can read it (versioned `state/fleet/coord_group.json`). The group carries only
non-secret claim records (message ids, node names, capacity scores).

Usage:
  provision_coordination_group.py --members <npub|hex>[,<npub>...] \
      [--name fleet-responder-ops] [--out PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
OPS = BOT / "hermes_ops.json"
ORANGE = "wss://relay.orangesync.tech"
NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                        "/usr/local/bin/nak", "/usr/bin/nak"]
            if Path(c).exists()), "nak")


def sh(args: list[str], timeout: int = 60) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def norm_pub(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    if p.startswith(("npub1", "nsec1", "nsec")):
        out = sh([NAK, "key", "public", p], 15).strip()
        return out or p
    return p


def fetch_groups(nsec: str) -> dict[str, str]:
    raw = subprocess.run(
        [NAK, "req", "--auth", "--sec", nsec, ORANGE],
        input=json.dumps({"kinds": [39000]}) + "\n",
        capture_output=True, text=True, timeout=60).stdout
    mapping: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) > 1}
        if tags.get("name") and tags.get("d"):
            mapping.setdefault(tags["name"], tags["d"])
    return mapping


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="fleet-responder-ops")
    ap.add_argument("--members", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = {}
    try:
        cfg = json.loads(OPS.read_text())
    except Exception:
        pass
    bridge_nsec = Path(os.path.expanduser(
        cfg.get("node_nsec", "~/.hermes/keys/hermes-ops/cobrador.nsec")))
    if not bridge_nsec.exists():
        print(f"error: bridge nsec not found ({bridge_nsec})", file=sys.stderr)
        return 2
    nsec = bridge_nsec.read_text().strip()

    members = [norm_pub(m) for m in (args.members or "").split(",")]
    members = [m for m in members if m]
    if not members:
        for f in sorted((HOME / ".hermes/keys/hermes-ops").glob("*.nsec")):
            members.append(norm_pub(f.read_text().strip()))
        # fleet node identities learned from peer heartbeats (fleet_heartbeat
        # includes each node's `pubkey`), so new nodes are added automatically.
        for hb in sorted((BOT / "peers").glob("*.json")) if (BOT / "peers").exists() else []:
            try:
                p = json.loads(hb.read_text()).get("pubkey")
            except Exception:
                p = None
            if p:
                members.append(p)
    manager = HOME / ".hermes/profiles/manager/keys/nostr_nsec.txt"
    if manager.exists():
        members.append(norm_pub(manager.read_text().strip()))
    members = list(dict.fromkeys(members))

    mapping = fetch_groups(nsec)
    gid = mapping.get(args.name)
    if gid:
        print(f"exists: {args.name} = {gid}")
    else:
        sh([NAK, "event", "-k", "9007", "-t", f"name={args.name}", "--auth",
            "--sec", nsec, ORANGE], 45)
        time.sleep(3)
        mapping = fetch_groups(nsec)
        gid = mapping.get(args.name)
        if not gid:
            print(f"error: created {args.name} but could not resolve its id",
                  file=sys.stderr)
            return 1
        print(f"created: {args.name} = {gid}")

    for pk in members:
        sh([NAK, "event", "-k", "9000", "-t", f"h={gid}", "-p", pk, "--auth",
            "--sec", nsec, ORANGE], 30)

    record = {"name": args.name, "group": gid, "members": members,
              "updated": int(time.time())}
    for dest in [Path(os.path.expanduser(args.out)) if args.out else None,
                 BOT / "coord_group.json"]:
        if not dest:
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(record, indent=1))
        except OSError:
            pass
    print(json.dumps(record, indent=1) if args.json
          else f"{args.name}={gid} members={len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
