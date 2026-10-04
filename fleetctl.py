#!/usr/bin/env python3
"""fleetctl.py — operator CLI for the two-node fleet.

Thin wrapper over fleet_remediate.py: run an action on the local node or a
named peer over the existing SSH trust.

Usage:
  fleetctl.py status [--node NODE]
  fleetctl.py <action> [--node NODE] [--reason TEXT] [--request-id ID]
  fleetctl.py nodes                      # list configured peers + health ages
Actions: notify throttle freeze drain restart quarantine rollback
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
FLEET_CFG = BOT / "fleet.json"
REMEDIATE = HERMES / "scripts" / "fleet_remediate.py"

ACTIONS = ("notify", "throttle", "freeze", "drain", "restart", "quarantine",
           "rollback")


def _read_json(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _self() -> str:
    return _read_json(FLEET_CFG, {}).get("node") or socket.gethostname()


def _peer(node: str):
    cfg = _read_json(FLEET_CFG, {})
    return next((p for p in cfg.get("peers", []) if p.get("name") == node), None)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet operator CLI")
    ap.add_argument("verb")
    ap.add_argument("--node", default="")
    ap.add_argument("--reason", default="operator fleetctl")
    ap.add_argument("--request-id", default="")
    args = ap.parse_args(argv)

    if args.verb == "nodes":
        cfg = _read_json(FLEET_CFG, {})
        print(f"self: {_self()} ({cfg.get('role')})")
        for p in cfg.get("peers", []) or []:
            name = p.get("name")
            h = _read_json(BOT / "peers" / f"{name}.health.json",
                           _read_json(BOT / "peers" / f"{name}.json", {}))
            age = int(time.time() - float(h.get("ts", 0) or 0)) if h else -1
            print(f"  {name}: role={h.get('role')} age={age}s "
                  f"workers={h.get('hermes_workers')} waste={h.get('waste_ratio')}")
        return 0

    target = args.node or _self()
    if target in ("self", _self()):
        cmd = [sys.executable, str(REMEDIATE), args.verb,
               "--reason", args.reason, "--request-id", args.request_id or "operator"]
    else:
        if not _peer(target):
            print(f"unknown node: {target}", file=sys.stderr)
            return 1
        sys.path.insert(0, str(REMEDIATE.parent))
        from fleet_remediate import peer_ssh  # type: ignore
        cmd = peer_ssh(target, args.verb, args.reason, args.request_id or "operator")
        if not cmd:
            print(f"cannot resolve peer {target}", file=sys.stderr)
            return 1
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    except Exception as exc:  # noqa: BLE001
        print(f"fleetctl error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
