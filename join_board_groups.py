#!/usr/bin/env python3
"""join_board_groups.py — ensure the Hermes identities are members of every
kanban board channel on OrangeSync (and the matching local strfry groups).

Each board has its own NIP-29 group; the agent must be a member to receive and
post. Uses the bridge key (owner of the groups it created) to add members on
OrangeSync, and the manager key (admin of the local groups) for the local relay.

Usage (on the bridge node):
  join_board_groups.py [--dry-run] [--orange-only] [--local-only] [--timeout 120]

Bounded by design: 72 boards × members × 2 relays is hundreds of `nak` calls;
with an unreachable relay each call could burn its per-call timeout and hang the
Ansible converge for hours ("process object is closed"). A global `--timeout`
budget (default 120 s) stops early and reports what was skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
INVENTORY = BOT / "board_groups.json"
ORANGE = "wss://relay.orangesync.tech"
LOCAL = "ws://100.90.101.9:7780"
NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
                        "/usr/bin/nak"] if Path(c).exists()), "nak")


def _run(cmd: list[str], timeout: float) -> str:
    """Run *cmd*, returning stdout. Never raises (relay/key errors are soft)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(1.0, timeout))
        return (r.stdout or "").strip()
    except Exception:
        return ""


def pub_of(path: Path) -> str:
    return _run([NAK, "key", "public", path.read_text().strip()], 15)


def norm_pub(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    if p.startswith(("npub1", "nsec1", "nsec")):
        return _run([NAK, "key", "public", p], 15) or p
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--orange-only", action="store_true")
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="global wall-clock budget in seconds (default 120)")
    ap.add_argument("--members", default="",
                    help="comma-separated fleet node pubkeys (hex/npub); "
                         "defaults to every ~/.hermes/keys/hermes-ops/*.nsec + manager")
    args = ap.parse_args()
    deadline = time.time() + max(1.0, args.timeout)

    inv = json.loads(INVENTORY.read_text())["boards"]

    # Member set comes from the fleet inventory (via --members) so a new node is
    # joined automatically; falls back to all node identities on this host.
    if args.members:
        members = [norm_pub(m) for m in args.members.split(",")]
        members = [m for m in members if m]
    else:
        members = []
        for p in sorted((HOME / ".hermes/keys/hermes-ops").glob("*.nsec")):
            members.append(pub_of(p))
        mgr = HOME / ".hermes/profiles/manager/keys/nostr_nsec.txt"
        if mgr.exists():
            members.append(pub_of(mgr))
    members = list(dict.fromkeys(members))

    ops_cfg = {}
    try:
        ops_cfg = json.loads((BOT / "hermes_ops.json").read_text())
    except Exception:
        pass
    bridge_path = Path(os.path.expanduser(
        ops_cfg.get("node_nsec", "~/.hermes/keys/hermes-ops/cobrador.nsec")))
    if not bridge_path.exists():
        bridge_path = HOME / ".hermes/keys/hermes-ops/cobrador.nsec"
    bridge_sec = bridge_path.read_text().strip()
    manager_sec = (HOME / ".hermes/profiles/manager/keys/nostr_nsec.txt").read_text().strip()

    total = 0
    skipped_boards = 0
    for slug, info in inv.items():
        if time.time() > deadline:
            skipped_boards = len(inv) - list(inv).index(slug)
            print(f"deadline {args.timeout:.0f}s reached — skipping "
                  f"{skipped_boards} remaining board(s)")
            break
        og = info.get("orange_group")
        lg = info.get("local_group")
        for pk in members:
            if time.time() > deadline:
                break
            budget = min(30.0, max(1.0, deadline - time.time()))
            if og and not args.local_only:
                total += 1
                if not args.dry_run:
                    _run([NAK, "event", "-k", "9000", "-t", f"h={og}", "-p", pk,
                          "--auth", "--sec", bridge_sec, ORANGE], budget)
            if time.time() > deadline:
                break
            budget = min(30.0, max(1.0, deadline - time.time()))
            if lg and not args.orange_only:
                total += 1
                if not args.dry_run:
                    _run([NAK, "event", "-k", "9000", "-t", f"h={lg}", "-p", pk,
                          "--sec", manager_sec, LOCAL], budget)
        print(f"{slug}: orange={og} local={lg} members={len(members)}")

    done = len(inv) - skipped_boards
    print(f"{'would add' if args.dry_run else 'added'} {total} memberships "
          f"across {done}/{len(inv)} boards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
