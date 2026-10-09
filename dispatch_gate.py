#!/usr/bin/env python3
"""dispatch_gate.py - one gate for local dispatch decisions.

Replaces the ad-hoc absolute `LOAD_THRESHOLD=3.4` check in staggered-dispatch.sh,
which had three defects:
  1. absolute loadavg, so it meant something different on every core count;
  2. loadavg includes uninterruptible IO wait, so a box doing disk work looked
     "loaded" while its CPUs idled;
  3. it was a SECOND gate disagreeing with the fleet's own policy
     (fleet.json: max_load_per_cpu / min_mem_available_mb).

Thresholds and the dispatch board list come from config/dispatch_policy.json.

Usage:
    dispatch_gate.py            # exit 0 = allow, 1 = deny; JSON on stdout
    dispatch_gate.py --boards   # print the configured board list, one per line
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_POLICY = Path(__file__).resolve().parent / "config" / "dispatch_policy.json"


def load_policy(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("DISPATCH_POLICY") or DEFAULT_POLICY)
    return json.loads(p.read_text())


def metrics() -> dict:
    load1 = float(open("/proc/loadavg").read().split()[0])
    mem = {}
    for line in open("/proc/meminfo"):
        k, _, v = line.partition(":")
        mem[k.strip()] = int(v.split()[0]) // 1024  # kB -> MB
    try:
        cores = int(subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip())
    except Exception:
        cores = os.cpu_count() or 1
    return {
        "load1": load1,
        "cores": max(cores, 1),
        "mem_available_mb": mem.get("MemAvailable", 0),
        "swap_total_mb": mem.get("SwapTotal", 0),
        "swap_free_mb": mem.get("SwapFree", 0),
    }


def decide(m: dict, policy: dict) -> dict:
    """Pure decision. Memory and swap veto; CPU throttles."""
    cores = max(int(m.get("cores") or 1), 1)
    per_cpu = float(policy.get("max_load_per_cpu", 0.8))
    limit = per_cpu * cores

    if m["mem_available_mb"] < float(policy.get("min_mem_available_mb", 1536)):
        return {"allow": False, "reason": "memory: %dMB available < %dMB floor"
                % (m["mem_available_mb"], policy["min_mem_available_mb"]),
                "detail": m, "limit": limit}

    total = int(m.get("swap_total_mb") or 0)
    if total > 0:
        used_pct = 100.0 * (total - int(m.get("swap_free_mb") or 0)) / total
        if used_pct >= float(policy.get("max_swap_used_pct", 90)):
            return {"allow": False, "reason": "swap: %.0f%% used >= %s%% ceiling"
                    % (used_pct, policy["max_swap_used_pct"]),
                    "detail": m, "limit": limit}

    if m["load1"] >= limit:
        return {"allow": False, "reason": "load: %.2f >= %.2f (%.2f/CPU on %d cores)"
                % (m["load1"], limit, per_cpu, cores),
                "detail": m, "limit": limit}

    return {"allow": True, "reason": "ok: load %.2f < %.2f, mem %dMB"
            % (m["load1"], limit, m["mem_available_mb"]), "detail": m, "limit": limit}


def main(argv: list[str]) -> int:
    policy = load_policy()
    if "--boards" in argv:
        for b in policy.get("boards", []):
            print(b)
        return 0
    verdict = decide(metrics(), policy)
    print(json.dumps(verdict))
    return 0 if verdict["allow"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
