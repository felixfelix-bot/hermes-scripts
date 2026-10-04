#!/usr/bin/env python3
"""fleet_rebalance.py — whitelisted, reversible load-rebalancing actions.

One-tap apply backend for the fleet load-imbalance alert (§13). Only the
actions below are permitted; every one is reversible and appended to
``~/.hermes/bot/fleet_rebalance.jsonl``:

  status          show current offload cap / freeze state / live workers
  pause           set the offload cap to 0 (stop taking NEW fleet work)
  resume          remove the offload cap file (restore the default)
  cap N           set the offload cap to N
  throttle        fleet_remediate throttle (admission cap=1)
  drain           fleet_remediate drain (SIGTERM excess, keeps oldest)

Runs on the local node by default; ``--node <name>`` performs the same action
on a configured peer over the existing SSH trust.

Usage: fleet_rebalance.py [--node N] <action> [arg] [--reason TEXT]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
CAPFILE = BOT / "fleet_offload_max"
FLEET_CFG = BOT / "fleet.json"
REMEDIATE = HERMES / "scripts" / "fleet_remediate.py"
LEDGER = BOT / "fleet_rebalance.jsonl"
FREEZE_MARKERS = (HERMES / "ESTOP", BOT / ".dispatch_frozen",
                  BOT / ".fleet_quarantine", BOT / ".fleet_offload_disabled")

ACTIONS = ("status", "pause", "resume", "cap", "throttle", "drain")


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write_atomic(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)


def _self() -> str:
    return _read(FLEET_CFG, {}).get("node") or os.uname().nodename


def _workers() -> int:
    n = 0
    slots = BOT / ".fleet_slots"
    if slots.is_dir():
        for f in slots.glob("*.slot"):
            try:
                os.kill(int(f.stem), 0)
                n += 1
            except ProcessLookupError:
                continue
            except (ValueError, PermissionError):
                n += 1
    return n


def _ledger(entry: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _remediate(action: str, reason: str) -> str:
    if not REMEDIATE.exists():
        return f"fleet_remediate.py missing at {REMEDIATE}"
    try:
        r = subprocess.run([sys.executable, str(REMEDIATE), action,
                            "--reason", reason, "--request-id",
                            f"rebalance-{int(time.time())}"],
                           capture_output=True, text=True, timeout=120)
        return (r.stdout or r.stderr).strip()[:200]
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"


def do(action: str, arg: str, reason: str) -> str:
    if action == "status":
        cap = CAPFILE.read_text().strip() if CAPFILE.exists() else "(default)"
        frozen = [p.name for p in FREEZE_MARKERS if p.exists()]
        return (f"node={_self()} offload_cap={cap} workers={_workers()} "
                f"frozen={frozen or 'none'}")
    if action == "pause":
        _write_atomic(CAPFILE, "0\n")
        return f"{_self()}: offload paused (cap=0)"
    if action == "resume":
        try:
            CAPFILE.unlink()
        except FileNotFoundError:
            pass
        return f"{_self()}: offload resumed (cap file removed)"
    if action == "cap":
        try:
            n = int(arg)
            if n < 0:
                raise ValueError
        except (TypeError, ValueError):
            return f"cap: invalid value {arg!r} (need a non-negative integer)"
        _write_atomic(CAPFILE, f"{n}\n")
        return f"{_self()}: offload cap set to {n}"
    if action in ("throttle", "drain"):
        return f"{_self()}: " + _remediate(action, reason)
    return f"unknown action {action!r}"


def _peer_ssh(node: str, action: str, arg: str, reason: str) -> list[str]:
    cfg = _read(FLEET_CFG, {})
    peer = next((p for p in cfg.get("peers", []) if p.get("name") == node), None)
    if not peer:
        return []
    hosts = peer.get("hosts") or ([peer["host"]] if peer.get("host") else [])
    if not hosts:
        return []
    key = os.path.expanduser(peer.get("key", "~/.ssh/id_dq05"))
    user = peer.get("user", "c03rad0r")
    remote = (f"python3 ~/.hermes/scripts/fleet_rebalance.py {action}"
              + (f" {arg}" if arg else "")
              + f" --reason {json.dumps(reason)}")
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=8", "-i", key, f"{user}@{hosts[0]}", remote]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet rebalancing actions")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--node", default="")
    ap.add_argument("--reason", default="operator rebalance via Buzz")
    args = ap.parse_args(argv)

    node = args.node or ""
    reason = args.reason
    if node and node != _self():
        cmd = _peer_ssh(node, args.action, args.arg, reason)
        if not cmd:
            print(f"[fleet-rebalance] unknown or unreachable peer {node!r}")
            return 1
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            result = (r.stdout or r.stderr).strip()[:200] or f"rc={r.returncode}"
        except Exception as exc:  # noqa: BLE001
            result = f"peer error:{exc}"
            _ledger({"ts": time.time(), "node": node, "action": args.action,
                     "arg": args.arg, "result": result, "ok": False})
            print(f"[fleet-rebalance] {node} {args.action}: {result}")
            return 1
        _ledger({"ts": time.time(), "node": node, "action": args.action,
                 "arg": args.arg, "result": result, "ok": True})
        print(f"[fleet-rebalance] {node} {args.action}: {result}")
        return 0

    result = do(args.action, args.arg, reason)
    ok = not result.startswith("unknown") and "invalid value" not in result
    _ledger({"ts": time.time(), "node": _self(), "action": args.action,
             "arg": args.arg, "result": result, "ok": ok})
    print(f"[fleet-rebalance] {result}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
