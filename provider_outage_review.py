#!/usr/bin/env python3
"""provider_outage_review.py — recorded review-gate waiver during an outage.

D-128 requires a cold *cross-family* review. During a provider outage the
required family can be entirely unreachable, so the gate can never pass and
finished code sits blocked (the 2026-09-20 AV stall). Policy: while a family is
in outage, record a waiver (family + reason + since) for tasks whose only
missing gate is ``cold_cross_family_review``, and **auto-queue a re-review** so
the first real cross-family review still happens once the family returns.

Pure plan() + thin IO main. Config: state/fleet/provider_outage_review.json.

Usage:
  provider_outage_review.py --policy <json> --reachable kimi,glm,deepseek
      [--state <json>] [--rereview-queue <jsonl>] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_POLICY = {
    "gate": "cold_cross_family_review",
    "families": {"kimi": "moonshot", "glm": "zhipu",
                 "qwen": "alibaba", "deepseek": "deepseek"},
    "reason": "provider-outage",
}


def plan(policy: dict, reachable: set[str], state: dict) -> dict:
    """Compare reachable families against the policy; return new state + actions.

    ``reachable`` is a set of *provider/lane* names (e.g. ollama_cloud, deepseek).
    A family is in outage when none of its member lanes is reachable. Emits:
      * ``waivers[family]`` = {since, reason} while in outage (idempotent)
      * ``rereview``      = families that JUST recovered (need a real review)
    """
    fams = policy.get("families", DEFAULT_POLICY["families"])
    reason = policy.get("reason", "provider-outage")
    now = int(time.time())
    waivers = dict(state.get("waivers", {}))
    rereview_members = {m for m in fams}
    actions = {"new_outages": [], "recoveries": []}

    # A lane is "reachable for a family" if a member lane name appears in the
    # reachable set (families share provider names here for simplicity).
    def family_up(fam: str) -> bool:
        return any(m in reachable for m in (fam, fams.get(fam, fam)))

    for fam in fams:
        up = family_up(fam)
        had = fam in waivers
        if not up and not had:
            waivers[fam] = {"since": now, "reason": reason}
            actions["new_outages"].append(fam)
        elif up and had:
            del waivers[fam]
            actions["recoveries"].append(fam)

    return {"waivers": waivers, "updated": now,
            "gate": policy.get("gate", DEFAULT_POLICY["gate"]),
            "actions": actions}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True)
    ap.add_argument("--reachable", default="",
                    help="comma-separated reachable lane/provider names")
    ap.add_argument("--state", default=str(Path.home() / ".hermes/state/provider_outage_review.json"))
    ap.add_argument("--rereview-queue", default=str(Path.home() / ".hermes/state/review_rereview_queue.jsonl"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    pol = DEFAULT_POLICY
    try:
        pol = {**DEFAULT_POLICY, **json.loads(Path(args.policy).read_text())}
    except Exception:
        pass
    reachable = {s.strip() for s in args.reachable.split(",") if s.strip()}
    sp = Path(args.state)
    try:
        state = json.loads(sp.read_text())
    except Exception:
        state = {}
    new = plan(pol, reachable, state)
    print(f"[outage] new_outages={new['actions']['new_outages']} "
          f"recoveries={new['actions']['recoveries']}")
    if args.apply:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(new, indent=1))
        if new["actions"]["recoveries"]:
            with open(args.rereview_queue, "a") as fh:
                for fam in new["actions"]["recoveries"]:
                    fh.write(json.dumps({"family": fam, "ts": new["updated"],
                                         "reason": "provider-recovered"}) + "\n")
        print(f"[outage] wrote {sp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
