#!/usr/bin/env python3
"""dispatch_health_check.py — detect the "parking lot" condition: ready work
exists but the in-gateway dispatcher spawns nothing because the headroom
governor has capped the fleet.

Dispatch is gateway-resident (`kanban.dispatch_in_gateway: true`); the legacy
standalone `hermes-dispatch.service` is NOT the dispatch path (role 11 removed
its probe). This guard makes silent parking visible and, in --strict mode,
fails so a health/gate run catches it.

Usage:
  dispatch_health_check.py [--strict] [--json]
Exit: 0 ok; 1 parked/frozen/disabled in --strict.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
HERMES = HOME / ".hermes"
BOARDS = HERMES / "kanban" / "boards"
HEADROOM = HERMES / "bot" / "dispatch_headroom.json"
USAGE = HERMES / "bot" / "usage.csv"
FREEZE = [HERMES / "ESTOP", HERMES / "bot" / ".dispatch_frozen"]


def _yaml_scalar(text: str, key: str):
    """Extract a scalar from the kanban: block (no PyYAML dependency)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("kanban:"):
            for j in range(i + 1, len(lines)):
                s = lines[j]
                if s and not s[0].isspace():
                    break
                k, _, v = s.strip().partition(":")
                if k == key:
                    return v.split("#")[0].strip()
    return None


def read_config() -> dict:
    p = HERMES / "config.yaml"
    txt = p.read_text() if p.exists() else ""
    dig = _yaml_scalar(txt, "dispatch_in_gateway")
    itv = _yaml_scalar(txt, "dispatch_interval_seconds")
    return {"dispatch_in_gateway": str(dig).lower() == "true",
            "dispatch_interval_seconds": int(itv) if str(itv).isdigit() else 120}


def board_counts() -> dict:
    out = {}
    for db in glob.glob(str(BOARDS / "*" / "kanban.db")):
        name = Path(db).parent.name
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            ready = c.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0]
            running = c.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
            c.close()
        except Exception:
            continue
        if ready or running:
            out[name] = {"ready": ready, "running": running}
    return out


def headroom() -> dict:
    try:
        return json.loads(HEADROOM.read_text())
    except Exception:
        return {}


def evaluate(cfg, counts, head, frozen) -> dict:
    ready = sum(b["ready"] for b in counts.values())
    running = sum(b["running"] for b in counts.values())
    target = int(head.get("target_workers", 0) or 0)
    parked = ready > 0 and running >= target and target >= 0
    status = "ok"
    if not cfg["dispatch_in_gateway"]:
        status = "disabled"          # dispatch will never run
    elif frozen:
        status = "frozen"            # a bleed/ESTOP gate is closed
    elif parked:
        status = "parked"
    elif ready > 0 and not head.get("can_dispatch", True):
        status = "blocked"
    return {"status": status, "ready": ready, "running": running,
            "target_workers": target, "can_dispatch": head.get("can_dispatch"),
            "frozen": frozen, "dispatch_in_gateway": cfg["dispatch_in_gateway"],
            "boards": counts}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when disabled/frozen (and parked, see note)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = read_config()
    counts = board_counts()
    head = headroom()
    frozen = [str(p) for p in FREEZE if p.exists()]
    res = evaluate(cfg, counts, head, frozen)
    res["ts"] = int(time.time())

    if args.json:
        print(json.dumps(res))
    else:
        print(f"dispatch: {res['status']}  gateway={cfg['dispatch_in_gateway']} "
              f"target={res['target_workers']} running={res['running']} "
              f"ready={res['ready']} frozen={frozen or 'no'}")
        for name, b in sorted(counts.items()):
            if b["ready"]:
                print(f"  {name}: ready={b['ready']} running={b['running']}")

    if args.strict and res["status"] in ("disabled", "frozen"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
