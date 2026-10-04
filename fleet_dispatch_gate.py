#!/usr/bin/env python3
"""fleet_dispatch_gate.py — D-131 shared-headroom pre-spawn gate for local
kanban dispatch.

Before a node's local dispatcher spawns a worker for a ready card, it asks this
gate whether to proceed or to **yield** the card to a materially more-idle peer
(so the fleet offload queue can take it). Combined with advertise-all + `hold` +
a hold TTL, this makes local dispatch fleet-aware without disabling it.

Pure logic lives in `fleet_queue.should_yield`; this module is the thin
convenience wrapper + CLI other components (dispatchers, review router, CI
controller) call.

Exit codes: 0 = allow (dispatch locally), 3 = yield (leave for a peer).
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
FIT = BOT / "fleet_fit.json"

ALLOW = "allow"
YIELD = "yield"


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def local_view() -> tuple[str, dict, list[dict], dict]:
    """Return (node, my_health, peer_healths, my_fit) from the shared files."""
    hb = _read(BOT / "fleet_heartbeat.json", {}) or {}
    me = hb.get("node") or _read(BOT / "fleet.json", {}).get("node") or os.uname().nodename
    peers = []
    pdir = BOT / "peers"
    if pdir.exists():
        best: dict[str, dict] = {}
        for f in sorted(pdir.glob("*.json")):
            d = _read(f, None)
            if not (isinstance(d, dict) and d.get("ts") and
                    ("load1_per_cpu" in d or "headroom_score" in d)):
                continue
            n = d.get("node") or f.stem
            if n == me:
                continue
            if n not in best or d.get("ts", 0) >= best[n].get("ts", 0):
                best[n] = d
        peers = list(best.values())
    return me, hb, peers, _read(FIT, {})


def gate(task: dict, me: str, my_health: dict, peers: list[dict],
         my_fit: dict, now: float | None = None,
         margin: float | None = None) -> tuple[str, str]:
    """Return (ALLOW|YIELD, reason)."""
    now = now if now is not None else time.time()
    ok, why = fq.fit_ok(my_fit, task)
    if not ok:
        return ALLOW, f"unfit-here: {why}"  # let the local dispatcher decide
    kw = {}
    if margin is not None:
        kw["margin"] = margin
    peer = fq.should_yield(task, my_health, peers, my_fit, now, **kw)
    if peer:
        return YIELD, f"yield->{peer}(headroom)"
    return ALLOW, "allow"


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="")
    ap.add_argument("--task", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--margin", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    me, my_hb, peers, my_fit = local_view()
    repo = args.repo or args.board
    task = fq.classify(args.board, repo, args.title or args.task, args.body,
                       explicit_tags=f"repo:{repo}" if repo else "")
    decision, reason = gate(task, me, my_hb, peers, my_fit, margin=args.margin)
    out = {"node": me, "task": args.task, "decision": decision, "reason": reason,
           "peers": [p.get("node") for p in peers]}
    if args.json:
        print(json.dumps(out))
    else:
        print(f"{decision}: {reason}")
    return 0 if decision == ALLOW else 3


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
