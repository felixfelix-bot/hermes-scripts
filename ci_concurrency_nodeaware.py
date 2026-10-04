#!/usr/bin/env python3
"""ci_concurrency_nodeaware.py — D-131 node-local CI concurrency.

Replaces the cross-node `kalman-ci-concurrency` behaviour (which recreated the
peer's coordinator container — D-128 8.3) with a purely **local** controller:
each node derives its own `NGIT_CI_MAX_CONCURRENT_JOBS` from its own headroom and
writes it to `~/.hermes/bot/ngit_ci_max_jobs`. Runs are steered between nodes by
the self-assigning `ci_lease.py`, not by one node reaching into another.

CLI: ci_concurrency_nodeaware.py [--configured N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_queue as fq  # type: ignore

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
OUT = BOT / "ngit_ci_max_jobs"


def desired_jobs(headroom: float, configured_max: int,
                 min_jobs: int = 0) -> int:
    """Scale concurrency 0..configured_max with spare capacity.

    <floor -> min_jobs; >= 0.5 -> full; linear between. Pure/testable."""
    try:
        configured_max = max(0, int(configured_max))
    except (TypeError, ValueError):
        configured_max = 1
    if headroom <= 0.05:
        return max(0, min_jobs)
    if headroom >= 0.5:
        return configured_max
    scaled = round(configured_max * (headroom / 0.5))
    return max(min_jobs, min(configured_max, scaled))


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def local_headroom() -> float:
    return fq.headroom(_read(BOT / "fleet_heartbeat.json", {}))


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configured", type=int,
                    default=int(os.environ.get("NGIT_CI_CONFIGURED_MAX", "3")))
    ap.add_argument("--min", type=int, default=int(os.environ.get("NGIT_CI_MIN", "0")))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    head = local_headroom()
    jobs = desired_jobs(head, args.configured, args.min)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(str(jobs) + "\n")
    except OSError:
        pass
    out = {"node": fq._self(), "headroom": round(head, 3),
           "configured_max": args.configured, "jobs": jobs}
    print(json.dumps(out) if args.json else out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
