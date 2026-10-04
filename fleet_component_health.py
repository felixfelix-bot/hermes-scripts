#!/usr/bin/env python3
"""fleet_component_health.py — per-node Hermes-component health (Phase S).

The existing sensors (fleet_health/heartbeat) report *resource pressure*; this
reports whether the **Hermes setup on this node is working**: gateway, workers,
kanban sync freshness, the operator responder, the FIPS mesh, the self-heal
timers, and the profile `state.db` integrity. It is the sensor half of the loop;
`fleet_component_guard.py` is the repair half and `fleet_remediate.py` performs
the actions.

Output (stdout, JSON):
  {"node": "...", "ts": 123.4, "components": {
      "gateway":     {"status": "ok|degraded|down", "detail": "...", ...},
      "workers":     {...}, "kanban_sync": {...}, "responder": {...},
      "fips":        {...}, "timers": {...}, "state_db": {...}}}

Usage:
  fleet_component_health.py [--json] [--merge]
    --merge  merge the components block into $HERMES_HOME/bot/fleet_health.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
HEALTH_JSON = BOT / "fleet_health.json"
KANBAN_GIT_STATE = BOT / "kanban_git_state.json"
RESPONDER_STATE = BOT / "buzz_responder_state.json"
HEADROOM = BOT / "dispatch_headroom.json"
LOCKS = BOT / ".locks"
FIPS_PEERS_ALLOW = Path("/etc/fips/peers.allow")
FIPSCTL = "/usr/bin/fipsctl"

# Freshness / threshold knobs (seconds unless noted).
KANBAN_SYNC_STALE_S = int(os.environ.get("COMPONENT_KANBAN_STALE_S", "1800"))
RESPONDER_SLA_S = int(os.environ.get("COMPONENT_RESPONDER_SLA_S", "900"))
TIMER_STALE_S = int(os.environ.get("COMPONENT_TIMER_STALE_S", "1800"))
WORKER_PATTERN = os.environ.get("COMPONENT_WORKER_PATTERN", "work kanban task")


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    env = dict(os.environ)
    # `systemctl --user` needs a session env; inject it for cron/non-session runs.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"error:{exc}"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# probes — each returns {status, detail, ...}
# --------------------------------------------------------------------------- #
def probe_gateway() -> dict:
    code, out = _run(["systemctl", "--user", "show", "hermes-gateway.service"])
    props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    load = props.get("LoadState", "")
    if load in ("", "not-found", "masked"):
        return {"status": "ok", "detail": "gateway not installed on this node"}
    active = props.get("ActiveState", "unknown")
    sub = props.get("SubState", "")
    pid = props.get("MainPID", "0")
    restarts = props.get("NRestarts", "?")
    status = "ok" if active == "active" else "down"
    return {"status": status, "active": active, "sub": sub, "pid": pid,
            "restarts": restarts, "detail": f"hermes-gateway {active}/{sub} pid={pid} restarts={restarts}"}


def probe_workers() -> dict:
    code, out = _run(["pgrep", "-fc", WORKER_PATTERN])
    try:
        running = int(out) if out else 0
    except ValueError:
        running = 0
    cap = None
    capfile = _read_json(BOT / ".fleet_cap", {}) or {}
    if isinstance(capfile, dict) and capfile.get("cap"):
        cap = int(capfile["cap"])
    headroom = _read_json(HEADROOM, {}) or {}
    target = headroom.get("target_workers")
    parked = bool(headroom.get("parked")) or (
        isinstance(target, int) and target > running and running == 0)
    # A node with ready work but zero workers is degraded; otherwise ok.
    status = "degraded" if parked else "ok"
    return {"status": status, "running": running, "cap": cap,
            "target_workers": target, "parked": parked,
            "detail": f"workers={running} cap={cap} target={target} parked={parked}"}


def probe_kanban_sync() -> dict:
    state = _read_json(KANBAN_GIT_STATE, None)
    if isinstance(state, dict) and state.get("ts"):
        age = _now() - float(state["ts"])
        rc = state.get("rc")
        status = "down" if rc not in (0, None) else ("degraded" if age > KANBAN_SYNC_STALE_S else "ok")
        return {"status": status, "age_s": round(age, 1), "rc": rc,
                "head": state.get("head", "")[:12],
                "detail": f"kanban sync age={round(age)}s rc={rc}"}
    # Fallback: last commit time of the shared board repo.
    repo = os.path.expanduser("~/hermes-kanban")
    if Path(repo, ".git").exists():
        code, out = _run(["git", "-C", repo, "log", "-1", "--format=%ct"])
        if code == 0 and out.isdigit():
            age = _now() - float(out)
            return {"status": "degraded" if age > KANBAN_SYNC_STALE_S else "ok",
                    "age_s": round(age, 1), "source": "git",
                    "detail": f"board last commit age={round(age)}s"}
    if not (HERMES / "scripts" / "kanban_git.py").exists():
        return {"status": "ok", "detail": "kanban sync not installed on this node"}
    return {"status": "down", "detail": "no kanban sync state / board repo"}


def probe_responder() -> dict:
    state = _read_json(RESPONDER_STATE, {}) or {}
    leader = state.get("leader") or state.get("leader_node")
    last = state.get("last_reply_ts") or state.get("last_reply")
    self_node = _read_json(BOT / "fleet.json", {}).get("node")
    if last:
        age = _now() - float(last)
        status = "degraded" if age > RESPONDER_SLA_S else "ok"
        return {"status": status, "leader": leader, "age_s": round(age, 1),
                "detail": f"responder leader={leader} last_reply_age={round(age)}s"}
    # No reply time recorded: we cannot judge the SLA — stay ok so a quiet
    # channel never triggers a spurious re-election (auto-repair is enabled).
    return {"status": "ok", "leader": leader,
            "detail": f"responder leader={leader} (no reply history)"}


def probe_fips() -> dict:
    if not Path(FIPSCTL).exists():
        return {"status": "ok", "detail": "fips not installed on this node"}
    code, out = _run([FIPSCTL, "show", "peers"])
    if code != 0:
        return {"status": "down", "detail": "fipsctl show peers failed"}
    try:
        peers = json.loads(out).get("peers", [])
    except Exception:
        peers = []
    connected = [p for p in peers if p.get("connectivity") == "connected"]
    want = 0
    try:
        want = len([l for l in FIPS_PEERS_ALLOW.read_text().splitlines()
                    if l.strip() and not l.startswith("#")])
    except Exception:
        pass
    status = "ok" if connected else "degraded"
    return {"status": status, "connected": len(connected), "roster": want,
            "detail": f"fips connected={len(connected)} roster={want}"}


def probe_timers() -> dict:
    # Own-liveness via the state files the self-heal timers write (file mtime is
    # reliable from any context, unlike `systemctl --user` outside a session).
    files = {
        "fleet-health": BOT / "fleet_health.json",
        "fleet-arbiter": BOT / "fleet_arbiter_state.json",
        "fleet-deadman": BOT / "fleet_deadman_state.json",
        "state-db-guard": BOT / "state_db_health.json",
    }
    stale = []
    present = 0
    for name, p in files.items():
        try:
            age = _now() - p.stat().st_mtime
            present += 1
        except OSError:
            continue
        if age > TIMER_STALE_S:
            stale.append(f"{name}({int(age)}s)")
    if present == 0:
        return {"status": "ok", "detail": "self-heal timers not installed on this node"}
    status = "degraded" if stale else "ok"
    return {"status": status, "checked": len(files), "stale": stale,
            "detail": f"timer state stale={stale}"}


def probe_state_db() -> dict:
    """Read the last `state_db_health.py` result (no live quick_check here).

    Heavy integrity work runs on its own timer (state-db-guard.timer); this is a
    cheap file read so the 2-min component guard can decide to `repair-state-db`.
    A stale probe is a timer fault (probe_timers alerts on it), not DB damage, so
    it never triggers repair.
    """
    state = _read_json(BOT / "state_db_health.json", None)
    if not isinstance(state, dict) or not state.get("ts"):
        return {"status": "ok", "detail": "state.db probe not installed / no result yet"}
    age = _now() - float(state["ts"])
    corrupt = state.get("corrupt") or []
    if corrupt:
        return {"status": "down", "age_s": round(age, 1), "corrupt": corrupt,
                "detail": f"corrupt state.db: {','.join(corrupt)}"}
    max_age = int(os.environ.get("STATE_DB_PROBE_MAX_AGE_S", "3600"))
    if age > max_age:
        return {"status": "ok", "age_s": round(age, 1),
                "detail": f"state.db probe stale ({round(age)}s)"}
    return {"status": "ok", "age_s": round(age, 1),
            "unknown": state.get("unknown") or [], "detail": "state.db quick_check ok"}


PROBES = {
    "gateway": probe_gateway,
    "workers": probe_workers,
    "kanban_sync": probe_kanban_sync,
    "responder": probe_responder,
    "fips": probe_fips,
    "timers": probe_timers,
    "state_db": probe_state_db,
}


def probe_all() -> dict:
    comps = {}
    for name, fn in PROBES.items():
        try:
            comps[name] = fn()
        except Exception as exc:  # noqa: BLE001
            comps[name] = {"status": "down", "detail": f"probe error: {exc}"}
    node = _read_json(BOT / "fleet.json", {}).get("node") or os.uname().nodename
    return {"node": node, "ts": _now(), "components": comps}


def merge_into_health(result: dict) -> bool:
    try:
        health = _read_json(HEALTH_JSON, {}) or {}
        health["components"] = result["components"]
        health["components_ts"] = result["ts"]
        HEALTH_JSON.write_text(json.dumps(health, indent=2) + "\n")
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Hermes component health")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args(argv)
    result = probe_all()
    if args.merge:
        merge_into_health(result)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
