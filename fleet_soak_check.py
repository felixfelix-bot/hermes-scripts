#!/usr/bin/env python3
"""fleet_soak_check.py — 24h zero-storm soak monitor (Phase M).

Counts storm-guard interventions in the last 24h, records live pressure, and
writes ~/.hermes/logs/fleet-soak.json. Prints "SOAK FAIL" if any freeze/hard-kill
/ quarantine occurred in the window. Run every 30 min via systemd timer.

Usage: fleet_soak_check.py [--hours 24] [--json]

NOTE (D-128 0.5): reconstructed from the surviving __pycache__ bytecode after the
2026-09-11 fleet reorg dropped the untracked source. Behavior preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
LOG = HERMES / "logs" / "system-bleed-guard.log"
OUT = HERMES / "logs" / "fleet-soak.json"
HEALTH = HERMES / "bot" / "fleet_health.json"

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def scan(hours: int) -> dict:
    now = time.time()
    cutoff = now - hours * 3600
    counts = {"soft_freeze": 0, "hard_kill": 0, "quarantine": 0, "recover": 0}

    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []

    for line in lines[-20000:]:
        m = _TS.match(line)
        if not m:
            continue
        if _epoch(m.group(1)) < cutoff:
            continue
        low = line.lower()
        if "soft freeze" in low:
            counts["soft_freeze"] += 1
        elif "hard kill" in low:
            counts["hard_kill"] += 1
        elif "quarantine" in low:
            counts["quarantine"] += 1
        elif "clear" in low or "recover" in low:
            counts["recover"] += 1

    health = {}
    try:
        health = json.loads(HEALTH.read_text())
    except Exception:
        health = {}

    storms = counts["soft_freeze"] + counts["hard_kill"] + counts["quarantine"]
    verdict = "FAIL" if storms else "PASS"

    return {
        "ts": now,
        "iso": datetime.now(timezone.utc).isoformat(),
        "window_h": hours,
        "storms": storms,
        "counts": counts,
        "load1_per_cpu": health.get("load1_per_cpu"),
        "workers": health.get("hermes_workers"),
        "waste_ratio": health.get("waste_ratio"),
        "headroom_score": health.get("headroom_score"),
        "verdict": verdict,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    res = scan(args.hours)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1))

    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"SOAK {res['verdict']} (last {res['window_h']}h): "
              f"storms={res['storms']} {res['counts']} "
              f"workers={res['workers']} load/cpu={res['load1_per_cpu']} "
              f"waste={res['waste_ratio']}")
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
