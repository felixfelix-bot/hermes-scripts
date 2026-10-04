#!/usr/bin/env python3
"""fleet_arbiter.py — per-node health state machine + remediation decisions.

Reads this node's ``fleet_health.json`` and every ``peers/*.json`` heartbeat,
classifies each node, and decides a remediation intent from the ladder:

  L1 notify · L2 throttle · L3 freeze · L4 drain · L5 restart · L6 quarantine · L7 rollback

Safety rails (all required because authority is symmetric + full + auto-resume):
  * hysteresis  — SUSPECT/STORM need consecutive bad ticks; recovery needs clean ticks
  * dampening   — token-bucket per target (max actions / window, min cooldown)
  * fencing     — a signed-ish lease per target; only one actor acts per target
  * tie-break   — deterministic priority (stable role > canary), so peers running
                  this same logic agree on WHO acts and never double-actuate
  * healer-must-be-healthy — a node never remediates a peer while it is unhealthy

Default is ``--dry-run``. ``--apply`` executes intents via fleet_remediate.py
(self) or over SSH (peer). Every decision is appended to
``fleet_interventions.jsonl``.

Usage:
  fleet_arbiter.py [--apply] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
HEALTH = BOT / "fleet_health.json"
PEERS = BOT / "peers"
FLEET_CFG = BOT / "fleet.json"
STATE = BOT / "fleet_arbiter_state.json"
LEDGER = BOT / "fleet_interventions.jsonl"
LEASE_DIR = BOT / ".fleet_lease"
REMEDIATE = HERMES / "scripts" / "fleet_remediate.py"
SELF_PATH = BOT / "fleet_self.json"

ROLE_PRIORITY = {"stable": 0, "canary": 1, "worker": 2}
DEFAULTS = {
    # SUSPECT (informational) thresholds — deliberately soft so a normally
    # busy canary is not treated as a storm.
    "max_load_per_cpu": 0.8,
    "min_mem_available_mb": 1536,
    # STORM (actionable) thresholds — the genuine runaway signatures.
    "load_storm_per_cpu": 12.0,
    "max_workers": 4,
    "suspect_ticks": 1,
    "storm_ticks": 2,
    "clean_ticks": 3,
    # Burn/efficiency anomalies are ALERTS, not blocks (operator policy
    # 2026-09-13, "abnormal burn = alerts, never blocks").  A waste ratio at or
    # above waste_ratio_storm is a SOFT signal (SUSPECT → notify at most); only
    # an extreme ratio (waste_ratio_storm_hard) is actionable.  Before this the
    # soft threshold itself set storm=True, so a 3.0x cost-efficiency blip
    # froze dispatch — 50 freeze actions in 24h on cobrador, starving every
    # gated cron (Plebeian fix/review loops among them).
    "waste_ratio_storm": 3.0,
    "waste_ratio_storm_hard": 6.0,
    "max_actions": 3,
    "action_window_s": 1800,
    "cooldown_s": 300,
    "quarantine_cycles": 4,
    "quarantine_window_s": 6 * 3600,
    "stale_after_s": 180,
}


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write_json_atomic(p: Path, payload) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(p)


def _caps() -> dict:
    caps = dict(DEFAULTS)
    cfg = _read_json(FLEET_CFG, {})
    caps.update(cfg.get("caps", {}) or {})
    return caps


def _self_node() -> str:
    cfg = _read_json(FLEET_CFG, {})
    return cfg.get("node") or socket.gethostname()


def load_healths() -> list[dict]:
    """Local + peer health, deduped by node. When a node has both a raw
    heartbeat (peers/<n>.json) and a richer health file (peers/<n>.health.json),
    the richer/fresher one wins."""
    by_node: dict[str, dict] = {}

    def consider(h):
        if not h:
            return
        node = h.get("node") or "?"
        cur = by_node.get(node)
        if cur is None:
            by_node[node] = h
            return
        cur_rich = "waste_ratio" in cur
        new_rich = "waste_ratio" in h
        if (new_rich and not cur_rich) or (
            new_rich == cur_rich
            and float(h.get("ts", 0) or 0) > float(cur.get("ts", 0) or 0)
        ):
            by_node[node] = h

    me = _read_json(HEALTH, None)
    if me:
        me = dict(me)
        me.setdefault("node", _self_node())
        consider(me)
    for f in sorted(PEERS.glob("*.json")):
        consider(_read_json(f, None))
    return list(by_node.values())


def _max_workers(caps: dict, node: str) -> int:
    mw = caps.get("max_workers", 4)
    if isinstance(mw, dict):
        return int(mw.get(node, 4) or 4)
    return int(mw or 4)


def classify(h: dict, caps: dict, now: float) -> dict:
    """Pure: return {node, role, state, reasons, severity, metrics}."""
    node = h.get("node", "?")
    reasons: list[str] = []
    age = now - float(h.get("ts", 0) or 0)
    excluded = bool(h.get("role") == "excluded") or bool(h.get("maintenance"))

    cap_workers = _max_workers(caps, node)
    workers = int(h.get("hermes_workers", 0) or 0)
    load = float(h.get("load1_per_cpu", 0) or 0)
    mem = float(h.get("mem_available_mb", 1e9) or 1e9)
    waste = float(h.get("waste_ratio", 0) or 0)

    storm = False
    if age > caps["stale_after_s"]:
        reasons.append(f"stale ({int(age)}s)")
    # OPERATOR POLICY 2026-09-13: only a GENUINE RUNAWAY may freeze dispatch.
    # Being at worker capacity is normal, and high waste/burn is an ALERT-ONLY
    # signal. The pre-fix arbiter marked `workers>=cap` and waste>=hard as
    # STORM, producing 50 freeze actions/24h and starving every cron.
    if workers >= cap_workers:
        reasons.append(f"workers {workers}/{cap_workers} (soft)")
    if waste >= float(caps.get("waste_ratio_storm_hard", 6.0)):
        reasons.append(
            f"waste_ratio {waste}>= {caps.get('waste_ratio_storm_hard', 6.0)} (soft)")
    elif waste >= float(caps["waste_ratio_storm"]):
        reasons.append(f"waste_ratio {waste}>= {caps['waste_ratio_storm']} (soft)")
    # Genuine runaway signature — the ONLY actionable storm trigger.
    if load >= float(caps.get("load_storm_per_cpu", 12.0)):
        reasons.append(f"load/cpu {load}>= {caps.get('load_storm_per_cpu', 12.0)}")
        storm = True
    # Soft signals: informational only, so a normally busy node is SUSPECT not STORM.
    if (load >= float(caps["max_load_per_cpu"])
            and load < float(caps.get("load_storm_per_cpu", 12.0))):
        reasons.append(f"load/cpu {load}>= {caps['max_load_per_cpu']} (soft)")
    if mem < float(caps["min_mem_available_mb"]):
        reasons.append(f"mem {int(mem)}MB (soft)")
    if (waste >= float(caps["waste_ratio_storm"])
            and waste < float(caps.get("waste_ratio_storm_hard", 6.0))):
        reasons.append(f"waste_ratio {waste}>= {caps['waste_ratio_storm']} (soft)")

    if excluded:
        state = "HEALTHY"
    elif h.get("quarantined"):
        state = "QUARANTINE"
    elif storm:
        state = "STORM"
    elif reasons:
        state = "SUSPECT"
    else:
        state = "HEALTHY"
    return {"node": node, "role": h.get("role", "worker"), "state": state,
            "reasons": reasons, "metrics": {"workers": workers, "load": load,
            "mem": mem, "waste": waste}, "age_s": int(age)}


def _bump_state(state: dict, node: str, ok: bool, caps: dict) -> dict:
    ns = state.setdefault(node, {"bad_streak": 0, "clean_streak": 0,
                                 "cycles": [], "actions": []})
    if ok:
        ns["bad_streak"] = 0
        ns["clean_streak"] = int(ns.get("clean_streak", 0)) + 1
    else:
        ns["clean_streak"] = 0
        ns["bad_streak"] = int(ns.get("bad_streak", 0)) + 1
    return ns


def _damped(ns: dict, caps: dict, now: float) -> tuple[bool, str]:
    win = float(caps["action_window_s"])
    acts = [t for t in ns.get("actions", []) if now - float(t) <= win]
    ns["actions"] = acts
    if acts and now - float(acts[-1]) < float(caps["cooldown_s"]):
        return True, f"cooldown ({int(now - acts[-1])}s)"
    if len(acts) >= int(caps["max_actions"]):
        return True, f"action budget {len(acts)}/{caps['max_actions']}/{int(win)}s"
    return False, ""


def _priority(node: str, role: str) -> tuple:
    return (ROLE_PRIORITY.get(role, 9), node)


def decide(healths: list[dict], caps: dict, state: dict, me: str,
           now: float) -> list[dict]:
    """Pure decision core. Returns a list of intent dicts."""
    assessments = {a["node"]: a for a in (classify(h, caps, now) for h in healths)}
    healthy_nodes = [n for n, a in assessments.items() if a["state"] == "HEALTHY"]
    intents: list[dict] = []

    for node, a in assessments.items():
        ok = a["state"] == "HEALTHY"
        ns = _bump_state(state, node, ok, caps)
        if a["state"] == "HEALTHY":
            # NOTE: do NOT reset cycles on recovery — quarantine counts storm
            # episodes over a rolling window, and brief recoveries between them
            # are exactly the refill loop we are trying to brake. Stale cycles
            # age out via quarantine_window_s on the next storm evaluation.
            continue
        if a["state"] == "SUSPECT":
            # Informational only — a normally busy/canary node is not a storm.
            continue
        if ns["bad_streak"] < int(caps["storm_ticks"]):
            continue

        # Record a cycle only on the transition into storm (bad_streak first
        # reaches storm_ticks), so a single long storm is ONE cycle, not many.
        cyc = [t for t in ns["cycles"] if now - float(t) <= caps["quarantine_window_s"]]
        if ns["bad_streak"] == int(caps["storm_ticks"]):
            cyc.append(now)
        ns["cycles"] = cyc

        want_quarantine = len(cyc) >= int(caps["quarantine_cycles"])

        # Who acts? Self always may; peer only a healthy node with best priority.
        if node == me:
            actor_ok = True
        else:
            actor_ok = a["state"] != "QUARANTINE" and me in healthy_nodes
            if actor_ok:
                my_role = next((x["role"] for x in healths if x.get("node") == me),
                               "worker")
                mine = _priority(me, my_role)
                others = [_priority(n, assessments[n]["role"]) for n in healthy_nodes]
                actor_ok = bool(others) and mine == min(others)
        if not actor_ok:
            continue

        damped, why = _damped(ns, caps, now)
        if damped:
            intents.append({"target": node, "action": "hold", "level": 0,
                            "reason": why, "state": a["state"]})
            continue

        cap_workers = _max_workers(caps, node)
        if want_quarantine:
            action, level = "quarantine", 6
        elif a["state"] == "STORM":
            severe = a["metrics"]["workers"] >= cap_workers + 2
            action, level = ("drain", 4) if severe else ("freeze", 3)
        else:
            action, level = "notify", 1

        ns.setdefault("actions", []).append(now)
        intents.append({
            "target": node, "action": action, "level": level,
            "reason": "; ".join(a["reasons"]) or a["state"],
            "state": a["state"], "actor": me,
            "request_id": uuid.uuid4().hex[:12],
        })
    return intents


def _lease_path(target: str) -> Path:
    return LEASE_DIR / f"{target}.json"


def _try_lease(target: str, actor: str, request_id: str, ttl: int = 600) -> bool:
    """Best-effort single-actor lease. Expired leases may be taken over."""
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    p = _lease_path(target)
    now = time.time()
    existing = _read_json(p, None)
    if existing and now - float(existing.get("ts", 0)) < float(existing.get("ttl", 0)):
        return existing.get("actor") == actor
    _write_json_atomic(p, {"actor": actor, "request_id": request_id,
                           "ts": now, "ttl": ttl})
    return True


def _ledger(entry: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _apply(intent: dict, me: str) -> str:
    if intent["level"] <= 0:
        return "noop"
    if intent["target"] == me:
        cmd = [sys.executable, str(REMEDIATE), intent["action"],
               "--reason", intent["reason"], "--request-id", intent["request_id"]]
    else:
        from fleet_remediate import peer_ssh  # type: ignore
        cmd = peer_ssh(intent["target"], intent["action"],
                       intent["reason"], intent["request_id"])
    if not cmd or not Path(REMEDIATE).exists():
        return "no-remediator"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return f"rc={r.returncode} {(r.stdout or r.stderr).strip()[:120]}"
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet health arbiter")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    caps = _caps()
    now = time.time()
    me = _self_node()
    healths = load_healths()
    state = _read_json(STATE, {})
    intents = decide(healths, caps, state, me, now)

    applied = []
    if args.apply:
        for it in intents:
            if it["level"] <= 0:
                continue
            if not _try_lease(it["target"], me, it["request_id"]):
                it = dict(it, result="lease-held")
            else:
                it = dict(it, result=_apply(it, me))
            applied.append(it)
            _ledger({"ts": now, "actor": me, **it})
    else:
        for it in intents:
            _ledger({"ts": now, "actor": me, "dry_run": True, **it})

    _write_json_atomic(STATE, state)
    if args.json:
        print(json.dumps({"now": now, "self": me, "intents": intents,
                          "applied": applied}, indent=1))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        rows = applied if args.apply else intents
        print(f"[arbiter:{me}] {mode} intents={len(intents)}")
        for it in rows:
            print(f"  {it.get('state',''):11} {it['target']:9} L{it['level']} "
                  f"{it['action']:10} {it.get('reason','')} {it.get('result','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
