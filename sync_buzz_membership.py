#!/usr/bin/env python3
"""sync_buzz_membership.py — keep the operator + agent identities members of
every known OrangeSync NIP-29 channel (board groups + community groups).

Maintained replacement for the lost `buzz_channels.py` (D-128 §12.3 0.3).
Idempotent: state tracks (group, pubkey) pairs already added, so steady-state
runs send nothing.

Usage:
  sync_buzz_membership.py [--apply] [--dry-run]

Unit: buzz-channels-sync.service (hourly timer).
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
BOARD_GROUPS = BOT / "board_groups.json"
OPS_CFG = BOT / "hermes_ops.json"
STATE = HOME / ".hermes" / "state" / "buzz-membership.json"
ORANGE = "wss://relay.orangesync.tech"
NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                        "/usr/local/bin/nak", "/usr/bin/nak"]
            if Path(c).exists()), "nak")

OPERATOR_PUB = "1a31189f46e89d327e6a4fa26376ba5fa81caaec453fab13b1c2f8245e42ba9d"

# Community channels that are not kanban boards (from the retired
# .buzz_channels_plan.json). The operator should be a member of all of these.
COMMUNITY_GROUPS = {
    "1e8bffab-7cd7-42dc-a61c-bf275b91d8dc": "e2e",
    "4383bd1d-9bcf-447d-801a-1d8e5a980da3": "amperstrand",
    "e6616f1a-e0f5-5e3a-bd5d-76a2d8921922": "Protein-RNA interactome analysis",
    "b5f8f21d-07cb-58a8-a33c-7c9c1d4f08f7": "Protein-RNA go analysis",
    "b1a7238e-cc2b-5785-949b-6bc776bef76e": "welcome-everyone",
    "cfd04606-1a66-4945-a1e0-523d6644ba18": "sitarani",
    "19c16797-46a7-45a0-ba27-f478cfaf3dfc": "sitarani",
    "3b7a0097-9cf3-5383-9c11-33741ba19d70": "general",
    "cfdacaef-73cb-4c0d-bb7f-2010c8ff92c6": "OrangeSync Friends",
    "22615add-4e2d-4db0-a4de-243b3d3a48e7": "OrangeSync AI Lab",
    "8ce5ea86-3b8c-4376-ba7a-11354ae98643": "OrangeSync Core",
    "1389391e-6721-4a3f-9e44-2bbd2f26ddf0": "hermes-ops",
}


def log(*p) -> None:
    print("[buzz-membership]", *p, flush=True)


def pub_of(nsec_path: Path) -> str | None:
    try:
        nsec = nsec_path.read_text().strip()
    except OSError:
        return None
    if not nsec:
        return None
    try:
        r = subprocess.run([NAK, "key", "public", nsec],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None
    except Exception:
        return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def target_groups() -> dict[str, str]:
    groups: dict[str, str] = dict(COMMUNITY_GROUPS)
    inv = load_json(BOARD_GROUPS, {})
    for slug, info in (inv.get("boards", inv) or {}).items():
        gid = (info or {}).get("orange_group")
        if gid:
            groups[gid] = f"board-{slug}"
    return groups


FAIL_BACKOFF_S = 6 * 3600


def load_state() -> tuple[set[str], dict[str, int]]:
    d = load_json(STATE, {})
    return set(d.get("done", [])), dict(d.get("failed", {}))


def save_state(done: set[str], failed: dict[str, int]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"done": sorted(done), "failed": failed, "ts": int(time.time())}, indent=1))
    tmp.replace(STATE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_json(OPS_CFG, {})
    bridge_nsec_path = Path(os.path.expanduser(
        cfg.get("node_nsec", "~/.hermes/keys/hermes-ops/cobrador.nsec")))
    try:
        bridge_nsec = bridge_nsec_path.read_text().strip()
    except OSError:
        log("FATAL: bridge nsec not readable:", bridge_nsec_path)
        return 1

    members = [OPERATOR_PUB]
    for p in (HOME / ".hermes/profiles/manager/keys/nostr_nsec.txt",
              HOME / ".hermes/keys/hermes-ops/cobrador.nsec"):
        pub = pub_of(p)
        if pub and pub not in members:
            members.append(pub)

    groups = target_groups()
    done, failed = load_state()
    now = int(time.time())

    # Agents belong in board channels + the operator channel so Hermes can act
    # there; community channels get the operator only (no agent auto-replies).
    pairs: list[tuple[str, str, str]] = []
    for gid, name in groups.items():
        pubkeys = members if (name.startswith("board-") or name == "hermes-ops") \
            else [OPERATOR_PUB]
        for pub in pubkeys:
            pairs.append((gid, name, pub))
    missing = [(gid, name, pub) for gid, name, pub in pairs
               if f"{gid}:{pub}" not in done
               and now - failed.get(f"{gid}:{pub}", 0) > FAIL_BACKOFF_S]

    log(f"groups={len(groups)} members={len(members)} pairs={len(pairs)} "
        f"already={len(pairs) - len(missing)} missing={len(missing)}")

    if args.dry_run or not args.apply:
        for gid, name, pub in missing[:20]:
            log(f"  would add {pub[:10]}… to {name} ({gid})")
        if len(missing) > 20:
            log(f"  … +{len(missing) - 20} more")
        return 0

    added = 0
    for gid, name, pub in missing:
        r = subprocess.run(
            [NAK, "event", "-k", "9000", "-t", f"h={gid}", "-p", pub,
             "--auth", "--sec", bridge_nsec, ORANGE],
            capture_output=True, text=True, timeout=45)
        key = f"{gid}:{pub}"
        ok = "success" in (r.stdout + r.stderr)
        if ok:
            done.add(key)
            failed.pop(key, None)
            added += 1
        else:
            failed[key] = now
            log(f"  FAILED {name} {gid[:8]} {pub[:8]}: "
                f"{(r.stdout + r.stderr).strip()[-120:]}")
        time.sleep(0.15)

    save_state(done, failed)
    log(f"added {added}/{len(missing)}; state now {len(done)} pairs "
        f"({len(failed)} backed off)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
