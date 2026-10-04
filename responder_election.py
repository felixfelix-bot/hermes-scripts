#!/usr/bin/env python3
"""responder_election.py — deterministic, resource-scored election for the Buzz
responder fleet (D-129).

Several nodes (Klaus on DQ05, Felix on CW, ...) are members of the same Buzz
groups. Exactly one should answer each operator message. The nodes already share
Kalman-smoothed capacity over the SSH heartbeat transport, so each can
independently compute the *same* ranked list; a short claim handshake on a
private coordination group closes the race.

This module is pure (no I/O) so it is fully unit-testable
(tests/test_responder_election.py). The caller supplies heartbeats and claim
records.

Concepts
  score(hb)        0..1 spare-capacity score (min of CPU / memory / worker slots)
  rank(nodes)      deterministic ordering: alive, score desc, cap desc, name asc
  choose_leader    sticky incumbent unless a challenger beats it by hysteresis
  pick_winner      deterministic claim winner: headroom desc, ts asc, node asc
"""
from __future__ import annotations

import argparse
import json
import sys
import time

DEFAULT_STALE_S = 120.0
DEFAULT_MIN_HEADROOM = 0.05
DEFAULT_HYSTERESIS = 0.15
DEFAULT_CLAIM_TTL = 30.0
DEFAULT_MEM_FLOOR_MB = 1024.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _f(v, default) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _cap(hb: dict) -> float:
    """Best available worker cap for a node heartbeat."""
    for k in ("smoothed_cap", "fleet_cap"):
        if hb.get(k) is not None:
            return _f(hb.get(k), 4.0)
    return _f((hb.get("pressure") or {}).get("smoothed_cap"), 4.0)


def resource_score(hb: dict | None, *,
                   min_mem_mb: float = DEFAULT_MEM_FLOOR_MB) -> float:
    """Normalized 0..1 spare-capacity score for one heartbeat.

    Mirrors fleet_health.headroom_score: the min of spare CPU, spare memory and
    spare worker slots, with a hard floor when available memory is below
    `min_mem_mb` or the router is unhealthy. A node with no heartbeat scores 0.
    """
    if not hb:
        return 0.0
    router = hb.get("router") or {}
    if router and router.get("ok") is False:
        return 0.0
    load = _f(hb.get("load1_per_cpu"), 0.0)
    spare_cpu = _clamp(1.0 - load)

    total = _f(hb.get("mem_total_mb"), 0.0)
    avail = _f(hb.get("mem_available_mb"), 0.0)
    if avail and avail < min_mem_mb:
        return 0.0
    mem_spare = _clamp((_clamp(avail / total) - 0.10) / 0.50) if total else 0.0

    cap = _cap(hb)
    workers = _f(hb.get("hermes_workers"), 0.0)
    worker_spare = _clamp(1.0 - (workers / cap)) if cap else 0.0
    return round(min(spare_cpu, mem_spare, worker_spare), 3)


def rank(nodes: list[dict], now: float | None = None,
         stale_after_s: float = DEFAULT_STALE_S) -> list[dict]:
    """Order nodes for leadership.

    `nodes` is a list of {node, hb, ts}. Returns copies annotated with `alive`,
    `score`, `cap`; ordered alive-first, score desc, cap desc, node asc.
    """
    now = now if now is not None else time.time()
    out = []
    for n in nodes or []:
        name = n.get("node") or "?"
        hb = n.get("hb") or {}
        ts = _f(n.get("ts"), 0.0)
        alive = bool(hb) and (now - ts) <= stale_after_s and ts > 0
        out.append({
            "node": name, "alive": alive,
            "score": resource_score(hb) if alive else 0.0,
            "cap": _cap(hb) if hb else 0.0,
            "ts": ts,
        })
    out.sort(key=lambda r: (not r["alive"], -r["score"], -r["cap"], r["node"]))
    return out


def choose_leader(current: str | None, ranked: list[dict],
                  hysteresis: float = DEFAULT_HYSTERESIS) -> str | None:
    """Sticky leader: keep `current` unless it is gone/stale or the best alive
    challenger beats it by `hysteresis`."""
    alive = [r for r in ranked if r["alive"]]
    if not alive:
        return current
    best = alive[0]
    if current:
        cur = next((r for r in ranked if r["node"] == current), None)
        if cur and cur["alive"] and best["score"] < cur["score"] + hysteresis:
            return current
    return best["node"]


def eligible(ranked: list[dict], name: str,
             min_headroom: float = DEFAULT_MIN_HEADROOM) -> bool:
    r = next((x for x in ranked if x["node"] == name), None)
    return bool(r and r["alive"] and r["score"] >= min_headroom)


def pick_winner(claims: list[dict], now: float | None = None,
                ttl: float = DEFAULT_CLAIM_TTL) -> str | None:
    """Deterministic claim winner: freshest independent of arrival, then highest
    headroom, then earliest ts, then node name. Claims may name the node via
    `node` or `actor` (fleet_claim uses `actor`)."""
    now = now if now is not None else time.time()
    norm = []
    for c in (claims or []):
        node = c.get("node") or c.get("actor")
        if not node:
            continue
        norm.append({"node": node, "headroom": _f(c.get("headroom"), 0.0),
                     "ts": _f(c.get("ts"), 0.0)})
    fresh = [c for c in norm if (now - c["ts"]) <= ttl]
    if not fresh:
        return None
    fresh.sort(key=lambda c: (-c["headroom"], c["ts"], c["node"]))
    return fresh[0]["node"]


def tagged_pubkeys(ev: dict) -> set[str]:
    return {t[1] for t in (ev.get("tags") or []) if len(t) >= 2 and t[0] == "p"}


def is_direct(ev: dict, my_pubkeys) -> bool:
    return bool(tagged_pubkeys(ev) & set(my_pubkeys or []))


def tags_other_node(ev: dict, my_pubkeys, all_node_pubkeys) -> str | None:
    """If the message tags another fleet node (not me), return that pubkey."""
    others = set(all_node_pubkeys or []) - set(my_pubkeys or [])
    hit = tagged_pubkeys(ev) & others
    return sorted(hit)[0] if hit else None


def failover_due(pending: dict, now: float, grace_s: float,
                 tagged_alive: bool) -> bool:
    """A message tagged to a node that is stale becomes answerable by us after
    `grace_s`."""
    if tagged_alive:
        return False
    first = _f(pending.get("first_seen"), now)
    return (now - first) >= grace_s


def decide(*, ev: dict, my_pubkeys, all_node_pubkeys, leader: str | None,
           my_node: str, election: bool = True) -> str:
    """Return 'direct' | 'elected' | 'wait-tag' | 'skip'.

    * direct   — message tags one of my pubkeys: I answer now.
    * elected  — untagged (or fleet-wide) and I am the elected leader.
    * wait-tag — tags a different node; hold for the failover grace window.
    * skip     — I am not the leader / election disabled.
    """
    if is_direct(ev, my_pubkeys):
        return "direct"
    other = tags_other_node(ev, my_pubkeys, all_node_pubkeys)
    if other:
        return "wait-tag"
    if not election:
        return "skip"
    return "elected" if leader == my_node else "skip"


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Responder election — pure helper")
    ap.add_argument("--explain", action="store_true",
                    help="read fleet_heartbeat.json + peers/*.json and rank")
    args = ap.parse_args(argv)
    if not args.explain:
        ap.print_help()
        return 0
    import os
    from pathlib import Path
    bot = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "bot"
    local = {}
    try:
        local = json.loads((bot / "fleet_heartbeat.json").read_text())
    except Exception:
        pass
    nodes = [{"node": local.get("node", "self"), "hb": local,
              "ts": local.get("ts", 0)}]
    for f in sorted((bot / "peers").glob("*.json")) if (bot / "peers").exists() else []:
        try:
            hb = json.loads(f.read_text())
        except Exception:
            continue
        nodes.append({"node": hb.get("node", f.stem), "hb": hb,
                      "ts": hb.get("ts", 0)})
    ranked = rank(nodes)
    print(json.dumps({"leader": choose_leader(None, ranked), "ranked": ranked},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
