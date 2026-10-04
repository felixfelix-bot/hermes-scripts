#!/usr/bin/env python3
"""fleet_load_imbalance.py — alert when one node is disproportionately loaded.

Cross-node detector (§13 of PLAN-operator-channel.md). Reads the same data as
`fleet_status.collect()` (local `fleet_health.json` + `peers/*.health.json`),
compares each node's 1-minute load-per-cpu against the mean of the OTHERS, and
fires when one node is far above the rest — sustained across ticks, with a
cooldown latch.

On fire (production run):
  * posts a facts card to the Buzz `operator-alerts` channel;
  * dispatches a one-shot "load-balance consultant" kanban task (LLM only on
    fire — zero cost while balanced);
  * prints a one-line critical summary to stdout ONLY when severe, so a cron
    with `deliver=origin` mirrors it to the Signal admin group (Buzz primary,
    Signal critical-only).

Silent (empty stdout) while balanced. Pure-stdlib; each metric falls back safe.

Usage:
  fleet_load_imbalance.py [--json] [--dry-run] [--no-post] [--no-dispatch]
                          [--force] [--now T]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
STATE = HERMES / "state" / "fleet-load-imbalance.json"
CHANNEL_CFG = BOT / "alerts_channel.json"
DECISIONS_CFG = BOT / "decisions_channel.json"
DECISIONS_STATE = HERMES / "state" / "decisions_seen.json"
REPORTS = Path.home() / "reports"
BOARDS = HERMES / "kanban" / "boards"
NSEC = HERMES / "keys" / "hermes-ops" / "cobrador.nsec"

FLOOR = 0.5            # load/cpu floor so an idle fleet can't divide by ~0
RATIO_TH = 2.5         # hot / mean(others) ratio that counts as skew
ABS_TH = 2.0           # hot must clear this absolute load/cpu
SPREAD_TH = 1.0        # hot - baseline must clear this
MAX_AGE_S = 300        # ignore heartbeat older than this
SUSTAIN_TICKS = 2      # consecutive breaching ticks before firing
COOLDOWN_S = 3600      # don't re-alert (unchanged severity) inside this window
ESCALATE = 1.5         # a ratio this much worse re-fires inside cooldown
CRITICAL_RATIO = 8.0   # at/above this (or hot load/cpu) -> Signal mirror
CONSULTANT_MODEL = "deepseek-v4-pro"
CONSULTANT_BOARD = "hermes-orchestration"


def log(*p) -> None:
    print("[fleet-load]", *p, file=sys.stderr, flush=True)


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


# ── collection ────────────────────────────────────────────────────────────────

def load_nodes() -> list[dict]:
    """Reuse fleet_status.collect() so the node set matches every other tool."""
    try:
        import fleet_status  # type: ignore
        return fleet_status.collect().get("nodes", []) or []
    except Exception as exc:  # noqa: BLE001
        log("node collection failed:", exc)
        return []


# ── pure analysis ─────────────────────────────────────────────────────────────

def _age(n: dict, now: float) -> float:
    a = n.get("age_s")
    if a is not None:
        return float(a)
    return now - float(n.get("ts", 0) or 0)


def analyze(nodes: list[dict], now: float, floor: float = FLOOR,
            ratio_th: float = RATIO_TH, abs_th: float = ABS_TH,
            spread_th: float = SPREAD_TH, max_age: float = MAX_AGE_S) -> dict | None:
    """Return a skew facts dict, or None when the fleet is balanced."""
    fresh, stale = [], []
    for n in nodes:
        if n.get("load1_per_cpu") is None:
            continue
        age = _age(n, now)
        if age <= max_age:
            fresh.append(n)
        else:
            stale.append({"node": n.get("node"), "age_s": int(age)})
    if len(fresh) < 2:
        return None

    hot = max(fresh, key=lambda n: float(n["load1_per_cpu"]))
    cold = min(fresh, key=lambda n: float(n["load1_per_cpu"]))
    others = [n for n in fresh if n is not hot]
    baseline = statistics.mean(float(n["load1_per_cpu"]) for n in others)
    ratio = float(hot["load1_per_cpu"]) / max(baseline, floor)
    spread = float(hot["load1_per_cpu"]) - baseline

    if (ratio < ratio_th or float(hot["load1_per_cpu"]) < abs_th
            or spread < spread_th):
        return None

    def summ(n):
        return {"node": n.get("node"), "role": n.get("role"),
                "load1_per_cpu": n.get("load1_per_cpu"),
                "mem_available_mb": n.get("mem_available_mb"),
                "workers": n.get("workers"), "fleet_cap": n.get("fleet_cap"),
                "headroom_score": n.get("headroom_score")}

    return {
        "hot": hot.get("node"), "cold": cold.get("node"),
        "ratio": round(ratio, 2), "spread": round(spread, 2),
        "hot_load": round(float(hot["load1_per_cpu"]), 2),
        "baseline": round(baseline, 2),
        "nodes": [summ(n) for n in
                  sorted(fresh, key=lambda n: -float(n["load1_per_cpu"]))],
        "stale": stale,
    }


def is_critical(facts: dict) -> bool:
    return (float(facts.get("ratio", 0)) >= CRITICAL_RATIO
            or float(facts.get("hot_load", 0)) >= CRITICAL_RATIO)


def evaluate(state: dict, facts: dict | None, now: float,
             sustain: int = SUSTAIN_TICKS, cooldown: float = COOLDOWN_S,
             escalate: float = ESCALATE) -> tuple[str, dict]:
    """Update the latch and return one of clear|sustain|fire|cooldown."""
    if facts is None:
        state["bad_streak"] = 0
        state["last_facts"] = None
        return "clear", state
    state["bad_streak"] = int(state.get("bad_streak", 0)) + 1
    state["last_facts"] = facts
    last_fire = float(state.get("last_fire", 0) or 0)
    last_ratio = float(state.get("last_ratio", 0) or 0)
    if state["bad_streak"] < sustain:
        return "sustain", state
    if (now - last_fire < cooldown
            and float(facts["ratio"]) < last_ratio * escalate):
        return "cooldown", state
    state["last_fire"] = now
    state["last_ratio"] = float(facts["ratio"])
    return "fire", state


# ── rendering ─────────────────────────────────────────────────────────────────

def render_card(facts: dict, consultant_task: str | None = None) -> str:
    lines = [
        f"[FLEET-LOAD-IMBALANCE · P1] {facts['hot']} hot vs fleet baseline",
        f"Hot: {facts['hot']} load/cpu={facts['hot_load']} "
        f"(baseline {facts['baseline']}) ratio={facts['ratio']}x "
        f"spread={facts['spread']}",
        "Nodes:",
    ]
    for n in facts["nodes"]:
        lines.append(
            f"  {str(n['node']):10} role={str(n.get('role')):7} "
            f"load/cpu={n.get('load1_per_cpu')} mem={n.get('mem_available_mb')}MB "
            f"workers={n.get('workers')}/{n.get('fleet_cap')} "
            f"headroom={n.get('headroom_score')}")
    if facts.get("stale"):
        lines.append("Stale (excluded): " + ", ".join(
            f"{s['node']}({s['age_s']}s)" for s in facts["stale"]))
    lines.append(f"Recommend: route new/heavy work to {facts['cold']}; "
                 f"relieve {facts['hot']}")
    if consultant_task:
        lines.append(f"Consultant: {consultant_task}")
    else:
        lines.append("Consultant: (not dispatched)")
    return "\n".join(lines)


def render_critical(facts: dict) -> str:
    return (f"🚨 FLEET LOAD IMBALANCE: {facts['hot']} load/cpu={facts['hot_load']} "
            f"vs baseline {facts['baseline']} ({facts['ratio']}x) — route to "
            f"{facts['cold']}. Details on Buzz operator-alerts.")


def consultant_prompt(facts: dict, report_path: str) -> str:
    return (
        "You are a fleet load-balance consultant. A cross-node load imbalance "
        f"fired: {json.dumps(facts, sort_keys=True)}. Your job is to surface "
        "concrete, reversible ways to distribute load evenly across all Hermes "
        "nodes. (1) Decide whether this is REAL work skew or a routing/affinity "
        "artifact. Inspect: ~/.hermes/bot/fleet_queue_state.json (advertised / "
        "claims / winners), ~/.hermes/bot/fleet.json (per-node caps), "
        "~/.hermes/bot/fleet_fit.json (repo/capability locality — some work is "
        "pinned to one node), ~/.hermes/bot/fleet_interventions.jsonl (recent "
        "freeze/drain on the hot node), and the peer return path (fleet-done / "
        "write-back — an unproven return path can cause duplicate work). "
        "(2) Give ONE root-cause hypothesis with evidence. (3) Give THREE "
        "concrete reversible rebalancing options (e.g. raise the cold node's "
        "offload cap, drain excess on the hot node, temporarily mark the hot "
        "node maintenance so routing prefers the cold node), each with its "
        "expected effect and risk. (4) State explicitly what NOT to do. "
        "Advisory only — do not change routing yourself. Write your findings to "
        f"{report_path}.")


# ── side effects ──────────────────────────────────────────────────────────────

def post_channel(cfg: dict, text: str) -> bool:
    if not cfg or not cfg.get("orange_group"):
        return False
    if not NSEC.exists():
        log("bridge nsec missing — cannot post to Buzz")
        return False
    try:
        sec = NSEC.read_text().strip()
        r = subprocess.run(
            [_nak(), "event", "-k", "9", "-t", f"h={cfg['orange_group']}",
             "-c", text, "--auth", "--sec", sec,
             cfg.get("relay", "wss://relay.orangesync.tech")],
            capture_output=True, text=True, timeout=60)
        ok = "success" in (r.stdout + r.stderr)
        if not ok:
            log("Buzz post failed:", (r.stdout + r.stderr)[-160:])
        return ok
    except Exception as exc:  # noqa: BLE001
        log("Buzz post error:", exc)
        return False


def post_buzz(text: str) -> bool:
    return post_channel(_read(CHANNEL_CFG, {}), text)


def register_decision(facts: dict, now: float) -> str:
    """Register an actionable rebalance item in the operator-decisions state so
    `decisions_responder.py` can apply it on the operator's command."""
    did = "D-" + hashlib.sha1(
        f"fleet-load|{facts['hot']}|{int(now // 3600)}".encode()).hexdigest()[:8]
    st = _read(DECISIONS_STATE, {"items": {}})
    items = st.setdefault("items", {})
    if did not in items:
        items[did] = {
            "sig": json.dumps({"hot": facts["hot"],
                               "ratio": facts["ratio"]}, sort_keys=True),
            "first_seen": int(now), "last_posted": int(now),
            # 'advisory' (not 'open') so the generic digest does not later mark
            # it [RESOLVED]; it is resolved only when the operator acts.
            "status": "advisory",
            "item": {"kind": "fleet-load-imbalance",
                     "board": CONSULTANT_BOARD, "node": facts["hot"],
                     "title": f"rebalance {facts['hot']}",
                     "rebalance": {"node": facts["hot"], "sub": "pause"}},
        }
        _write(DECISIONS_STATE, st)
        text = (
            f"[{did} · P1] fleet-load-imbalance\n"
            f"What: {facts['hot']} load/cpu={facts['hot_load']} vs baseline "
            f"{facts['baseline']} ({facts['ratio']}x)\n"
            f"Recommend: relieve {facts['hot']}; route new work to {facts['cold']}\n"
            f"Action: reply `rebalance {did} pause` "
            f"(or: cap N | drain | throttle | resume)\n"
            f"Source: operator-alerts (Buzz)")
        post_channel(_read(DECISIONS_CFG, {}), text)
    return did


def dispatch_consultant(facts: dict, now: float) -> str | None:
    board = (CONSULTANT_BOARD if (BOARDS / CONSULTANT_BOARD).exists()
             else "router-maintenance")
    ts = time.strftime("%Y%m%d-%H%M", time.gmtime(now))
    report = str(REPORTS / f"fleet-load-imbalance-{ts}.md")
    REPORTS.mkdir(parents=True, exist_ok=True)
    title = f"[LOAD-IMBALANCE] {facts['hot']} {facts['ratio']}x — balance consultant"
    body = consultant_prompt(facts, report)
    try:
        r = subprocess.run(
            ["hermes", "kanban", "--board", board, "create",
             "--body", body, "--urgency", "soon", "--json", title],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log("kanban create failed:", r.stderr.strip()[:200])
            return None
        # The urgency gate prefixes a "classified [...]" line before the JSON,
        # so regex the task id first and only then fall back to parsing.
        m = re.search(r"\bt_[a-f0-9]{8}\b", r.stdout)
        tid = m.group(0) if m else None
        if not tid:
            try:
                d = json.loads(r.stdout)
                tid = d.get("id") or d.get("task_id")
            except Exception:
                tid = None
        if not tid:
            return None
        db = BOARDS / board / "kanban.db"
        if db.exists():
            try:
                c = sqlite3.connect(str(db), timeout=5)
                c.execute("update tasks set model_override=? where id=?",
                          (CONSULTANT_MODEL, tid))
                c.commit()
                c.close()
            except sqlite3.Error as exc:
                log("model_override failed:", exc)
        return tid
    except Exception as exc:  # noqa: BLE001
        log("consultant dispatch error:", exc)
        return None


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--now", type=float, default=None)
    args = ap.parse_args(argv)

    now = args.now if args.now is not None else time.time()
    facts = analyze(load_nodes(), now)
    state = _read(STATE, {})

    if args.dry_run:
        if args.json:
            print(json.dumps(facts, indent=1))
        elif facts is None:
            print("NO_ALERTS")
        else:
            print(render_card(facts))
        return 0

    decision, state = evaluate(state, facts, now)
    if args.force and facts is not None:
        decision = "fire"
    _write(STATE, state)

    if decision != "fire":
        log(f"decision={decision} (silent)")
        return 0

    task = None
    if not args.no_dispatch:
        task = dispatch_consultant(facts, now)
    if not args.no_post:
        post_buzz(render_card(facts, task))
        register_decision(facts, now)

    # Critical-only Signal mirror: stdout is delivered by the cron job. A
    # non-critical fire is Buzz-only, so stdout must stay empty.
    if is_critical(facts):
        print(render_critical(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
