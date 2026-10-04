#!/usr/bin/env python3
"""apply_node_caps.py — merge per-node governor caps into bot/fleet.json (W2.6).

Used to make a node "extra strict" as a dispatcher (e.g. build/relay VPSes):
lower static_cap / max_workers so the Kalman/arbiter governors keep it busy with
build/relay work instead of dispatched tasks. Idempotent; preserves other caps.

Usage: apply_node_caps.py --fleet-json PATH --node NODE [--static-cap N] [--max-workers N]
Prints 'changed' or 'ok'.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def apply_caps(path: Path, node: str, static_cap: int | None,
               max_workers: int | None) -> bool:
    try:
        d = json.loads(path.read_text())
    except Exception:
        d = {}
    caps = d.setdefault("caps", {})
    changed = False
    if static_cap is not None and caps.get("static_cap") != static_cap:
        caps["static_cap"] = static_cap
        changed = True
    if max_workers is not None:
        mw = caps.setdefault("max_workers", {})
        if mw.get(node) != max_workers:
            mw[node] = max_workers
            changed = True
    if changed:
        path.write_text(json.dumps(d, indent=1) + "\n")
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet-json", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--static-cap", type=int)
    ap.add_argument("--max-workers", type=int)
    a = ap.parse_args(argv)
    changed = apply_caps(Path(a.fleet_json), a.node, a.static_cap, a.max_workers)
    print("changed" if changed else "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
