#!/usr/bin/env python3
"""fleet_component_guard.py — repair half of the Phase-S component-health loop.

Reads the component status from `fleet_component_health.py` (or recomputes it)
and, after N consecutive bad ticks, triggers the mapped remediation verb via
`fleet_remediate.py`. Deliberately kept OFF the load/storm/quarantine ladder
(D-133) so a component fault never freezes dispatch.

Safety (auto-repair runs without operator approval):
  * kill-switch  — $HERMES_HOME/bot/.component_repair_off  disables all actions
  * budget       — at most BUDGET actions per WINDOW across all components
  * cooldown     — per-component minimum interval between actions
  * strikes      — act only after STRIKES consecutive bad observations
  * quarantine   — never act on a node already quarantined
  * every action is written to fleet_interventions.jsonl and alerts the operator

Usage:
  fleet_component_guard.py [--apply] [--json]
    (default is dry-run; --apply performs the actions)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
STATE = BOT / "component_guard_state.json"
LEDGER = BOT / "fleet_interventions.jsonl"
KILL = BOT / ".component_repair_off"
QUARANTINE = BOT / ".fleet_quarantine"
REMEDIATE = HERMES / "scripts" / "fleet_remediate.py"
HEALTH = HERE / "fleet_component_health.py"
# Fleet-wide policy (config-as-code via role 23). `enabled:false` is the operator
# kill-switch; strikes/cooldown/budget/window tune the guard per fleet.
CONFIG = BOT / "component_repair.json"

STRIKES = int(os.environ.get("COMPONENT_GUARD_STRIKES", "2"))
COOLDOWN_S = int(os.environ.get("COMPONENT_GUARD_COOLDOWN_S", "900"))
BUDGET = int(os.environ.get("COMPONENT_GUARD_BUDGET", "6"))
WINDOW_S = int(os.environ.get("COMPONENT_GUARD_WINDOW_S", "3600"))
ENABLED = True


def load_config() -> None:
    """Apply the fleet policy file over the env/default knobs (best-effort)."""
    global STRIKES, COOLDOWN_S, BUDGET, WINDOW_S, ENABLED
    cfg = _read_json(CONFIG, {}) or {}
    if "enabled" in cfg:
        ENABLED = bool(cfg["enabled"])
    for key, attr in (("strikes", "STRIKES"), ("cooldown_s", "COOLDOWN_S"),
                      ("budget", "BUDGET"), ("window_s", "WINDOW_S")):
        if isinstance(cfg.get(key), int):
            globals()[attr] = cfg[key]

# component -> remediation verb (None = alert only). Timers can't be self-fixed
# by a repair verb; a silent timer is an operator alert.
COMPONENT_ACTION = {
    "gateway": "restart-gateway",
    "workers": "reap-stale",
    "kanban_sync": "resync-kanban",
    "responder": "re-elect-responder",
    "fips": "restart-fips",
    "timers": None,
    # state.db health is decided by state_db_health.py on its own timer; the
    # repair itself is loss-aware and may decline (pages instead) — see
    # state_db_autorepair.py.
    "state_db": "repair-state-db",
}


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _ledger(entry: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load_status() -> dict:
    """Component status: recompute via the health script (single source of truth)."""
    try:
        r = subprocess.run([sys.executable, str(HEALTH), "--json"],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)
    except Exception:
        return {"components": {}}


def decide(status: dict, state: dict, now: float | None = None) -> list[dict]:
    """Pure decision core (unit-testable): returns the list of actions to take."""
    now = now if now is not None else time.time()
    actions: list[dict] = []
    comps = status.get("components", {})
    for name, info in comps.items():
        st = (info or {}).get("status", "ok")
        cstate = state.setdefault(name, {"bad_streak": 0, "last_action_ts": 0})
        bad = st in ("degraded", "down")
        cstate["bad_streak"] = cstate.get("bad_streak", 0) + 1 if bad else 0
        if not bad:
            continue
        verb = COMPONENT_ACTION.get(name)
        if cstate["bad_streak"] < STRIKES:
            continue
        if verb is None:
            actions.append({"component": name, "action": "notify",
                            "detail": (info or {}).get("detail", ""), "reason": "alert-only"})
            cstate["bad_streak"] = 0
            continue
        if now - cstate.get("last_action_ts", 0) < COOLDOWN_S:
            continue
        actions.append({"component": name, "action": verb,
                        "detail": (info or {}).get("detail", ""), "reason": f"{name} {st}"})
        cstate["last_action_ts"] = now
        cstate["bad_streak"] = 0
    return actions


def _budget_ok(state: dict, now: float) -> bool:
    hist = [t for t in state.get("_action_times", []) if now - t < WINDOW_S]
    state["_action_times"] = hist
    return len(hist) < BUDGET


def run(apply: bool) -> dict:
    load_config()
    status = load_status()
    state = _read_json(STATE, {}) or {}
    now = time.time()
    actions = decide(status, state, now)

    blocked = []
    if not ENABLED:
        blocked = ["config-disabled"]
    elif KILL.exists():
        blocked = ["kill-switch"]
    elif QUARANTINE.exists():
        blocked = ["quarantined"]
    elif not _budget_ok(state, now):
        blocked = ["budget"]

    result = {"ts": now, "apply": apply, "blocked": blocked, "actions": actions, "performed": []}
    if apply and not blocked:
        for a in actions:
            try:
                if a["action"] == "notify" or not Path(REMEDIATE).exists():
                    r = subprocess.run([sys.executable, str(REMEDIATE), "notify",
                                        "--reason", f"{a['component']}: {a['reason']}"], timeout=20)
                else:
                    r = subprocess.run([sys.executable, str(REMEDIATE), a["action"],
                                        "--reason", f"component-guard: {a['component']} ({a['detail']})",
                                        "--request-id", f"cguard-{int(now)}"], timeout=600)
                a["rc"] = r.returncode
            except Exception as exc:  # noqa: BLE001
                a["rc"] = f"error:{exc}"
            state.setdefault("_action_times", []).append(now)
            result["performed"].append(a)
            _ledger({"ts": now, "source": "component-guard", **a})
    _write_json(STATE, state)
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Component-health repair guard")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    print(json.dumps(run(args.apply), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
