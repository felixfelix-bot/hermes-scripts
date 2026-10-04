#!/usr/bin/env python3
"""ci_lease.py — D-131 self-assigning CI job lease (no coordinator SPOF).

Each CI run is a fleet task: nodes advertise the runs they can see, each claims
by current headroom, and the deterministic winner (`fleet_queue.resolve_claims`,
respecting started-sticky) runs it. Any node can go down without blocking the
rest of the fleet.

Transport is the existing private fleet bus (SSH ledger + relay), namespace
`fleet-ci-*`. CLI:

  ci_lease.py advertise --run ID --repo R [--class C]
  ci_lease.py claim     --run ID [--wait S] [--json]   # exit 0 if this node wins
  ci_lease.py winner    --run ID [--json]
  ci_lease.py done      --run ID
  ci_lease.py status    [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_queue as fq  # type: ignore

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"

ADV = "fleet-ci-task"
CLAIM = "fleet-ci-claim"
DONE = "fleet-ci-done"
CLAIM_TTL = 900


def _health_headroom() -> float:
    hb = {}
    try:
        hb = json.loads((BOT / "fleet_heartbeat.json").read_text())
    except Exception:
        pass
    return fq.headroom(hb)


def pick_ci_winner(claims: list[dict]) -> str | None:
    """Pure, testable: deterministic CI lease winner (started-sticky, then
    headroom desc, earliest ts, node name)."""
    if not claims:
        return None
    return fq.resolve_claims({"_": claims}).get("_")


def _all(tag: str) -> list[dict]:
    return fq.fetch(tag) + fq.fetch_private(tag)


def advertise(run: str, repo: str = "", klass: str = "medium") -> bool:
    node = fq._self()
    return fq.publish_any(
        {"type": ADV, "id": run, "repo": repo, "resource_class": klass,
         "actor": node, "created_ts": time.time()}, ADV, True)


def claims_for(run: str) -> list[dict]:
    out = []
    for c in _all(CLAIM):
        if (c.get("id") or c.get("run")) == run:
            out.append({"node": c.get("actor"), "ts": c.get("claim_ts", c.get("_ts")),
                        "headroom": c.get("headroom"), "started": c.get("started")})
    return out


def winner(run: str) -> str | None:
    return pick_ci_winner(claims_for(run))


def claim(run: str, wait: float = 2.0) -> tuple[bool, str | None]:
    node = fq._self()
    fq.publish_any({"type": CLAIM, "id": run, "actor": node,
                    "claim_ts": time.time(), "headroom": _health_headroom()},
                   CLAIM, True)
    if wait:
        time.sleep(wait)
    w = winner(run)
    return (w == node), w


def done(run: str, started: bool = False) -> bool:
    node = fq._self()
    return fq.publish_any({"type": DONE, "id": run, "actor": node,
                           "started": started, "ts": time.time()}, DONE, True)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("advertise"); p.add_argument("--run", required=True)
    p.add_argument("--repo", default=""); p.add_argument("--class", dest="klass", default="medium")
    p = sub.add_parser("claim"); p.add_argument("--run", required=True)
    p.add_argument("--wait", type=float, default=2.0); p.add_argument("--json", action="store_true")
    p = sub.add_parser("winner"); p.add_argument("--run", required=True); p.add_argument("--json", action="store_true")
    p = sub.add_parser("done"); p.add_argument("--run", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "advertise":
        print(f"advertised {args.run}: {advertise(args.run, args.repo, args.klass)}")
        return 0
    if args.cmd == "claim":
        ok, w = claim(args.run, args.wait)
        print(json.dumps({"won": ok, "winner": w}) if args.json
              else f"won={ok} winner={w}")
        return 0 if ok else 4
    if args.cmd == "winner":
        w = winner(args.run)
        print(json.dumps({"run": args.run, "winner": w}) if args.json else str(w))
        return 0
    if args.cmd == "done":
        print(f"done {args.run}: {done(args.run)}")
        return 0
    if args.cmd == "status":
        st = {"node": fq._self(), "headroom": _health_headroom(),
              "claims": len(_all(CLAIM)), "tasks": len(_all(ADV))}
        print(json.dumps(st) if args.json else st)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
