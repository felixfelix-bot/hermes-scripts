#!/usr/bin/env python3
"""fleet_status.py — fleet capacity + work-pool status for agents/operators (K.6).

Human or --json view of every node's headroom_score, role, fit, and the current
fleet queue, plus a routing recommendation. This is the CLI the `fleet-status`
skill tells agents to call, and it doubles as an MCP tool backend.

Usage:
  fleet_status.py [--json] [--recommend]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def collect() -> dict:
    cfg = _read(BOT / "fleet.json", {})
    me = cfg.get("node") or socket.gethostname()
    nodes = []
    local = _read(BOT / "fleet_health.json", None)
    if local:
        nodes.append(local)
    for f in sorted((BOT / "peers").glob("*.json")):
        h = _read(f, None)
        if h and "headroom_score" in h:
            nodes.append(h)
    by = {}
    for n in nodes:
        nm = n.get("node")
        if nm:
            by[nm] = n
    nodes = list(by.values())
    now = time.time()
    out_nodes = []
    for n in nodes:
        out_nodes.append({
            "node": n.get("node"),
            "role": n.get("role"),
            "headroom_score": n.get("headroom_score"),
            "workers": n.get("hermes_workers"),
            "fleet_cap": n.get("fleet_cap"),
            "load1_per_cpu": n.get("load1_per_cpu"),
            "mem_available_mb": n.get("mem_available_mb"),
            "waste_ratio": n.get("waste_ratio"),
            "age_s": int(now - float(n.get("ts", 0) or 0)),
            "max_class": (n.get("fit") or {}).get("max_class"),
            "exclusions": (n.get("fit") or {}).get("exclusions"),
            "repos": len((n.get("fit") or {}).get("repos", [])),
        })
    best = max((n for n in out_nodes if n["age_s"] < 300),
               key=lambda n: (n.get("headroom_score") or 0), default=None)
    state = _read(BOT / "fleet_queue_state.json", {"tasks": {}})
    return {"self": me, "nodes": out_nodes, "best_node": best["node"] if best else None,
            "queue_size": len(state.get("tasks", {}))}


def recommend(d: dict) -> str:
    b = next((n for n in d["nodes"] if n["node"] == d["best_node"]), None)
    if not b:
        return "No node has fresh capacity data."
    return (f"Route resource-intensive (heavy) work to {b['node']} "
            f"(headroom_score={b['headroom_score']}, repos={b['repos']}). "
            f"Keep experiments/canary-only and local-key tasks on their origin node. "
            f"Fleet queue depth: {d['queue_size']}.")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--recommend", action="store_true")
    args = ap.parse_args(argv)
    d = collect()
    if args.json:
        print(json.dumps(d, indent=1))
        return 0
    print(f"Fleet status (self={d['self']})")
    for n in sorted(d["nodes"], key=lambda x: -(x.get("headroom_score") or 0)):
        print(f"  {n['node']:9} role={n['role']:7} headroom={n['headroom_score']} "
              f"workers={n['workers']}/{n['fleet_cap']} load/cpu={n['load1_per_cpu']} "
              f"waste={n['waste_ratio']} repos={n['repos']} "
              f"excl={n['exclusions']} age={n['age_s']}s")
    print(f"  best headroom: {d['best_node']}   queue={d['queue_size']}")
    if args.recommend:
        print("  recommend: " + recommend(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
