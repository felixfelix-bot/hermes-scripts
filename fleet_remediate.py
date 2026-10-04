#!/usr/bin/env python3
"""fleet_remediate.py — the fleet remediation ladder (idempotent, logged).

Runs on the TARGET node and is invoked either locally (self-heal) or over the
existing SSH trust by a peer (``fleetctl``/arbiter). Every action is safe to
re-run and never touches in-flight work except ``drain`` (bounded) and
``quarantine`` (freeze only — does not kill).

Actions:
  notify              L1  write an anomaly event (operator visibility)
  throttle            L2  cap admission to 1 via ~/.hermes/bot/.fleet_cap
  resync-kanban       L2  run kanban_git.py sync + record freshness state
  re-elect-responder  L2  force the operator responder to re-derive leadership
  freeze              L3  write ESTOP + .dispatch_frozen (hold NEW dispatch)
  drain               L4  SIGTERM excess workers beyond the cap (keep oldest)
  reap-stale          L4  prune dead worker slots / stale locks
  restart             L5  restart zai-proxy (optionally gateway with --allow-gateway)
  restart-gateway     L5  idle-gated restart of hermes-gateway (component repair)
  restart-fips        L5  restart the FIPS mesh (component repair)
  repair-state-db     L5  loss-aware offline state.db recovery (component repair)
  quarantine          L6  hard hold: ESTOP + .dispatch_frozen + .fleet_quarantine
  rollback            L7  restore last-known-good config/scripts via fleet_lkg.py

Usage:
  fleet_remediate.py <action> [--reason TEXT] [--request-id ID]
  fleet_remediate.py status
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
ESTOP = HERMES / "ESTOP"
FROZEN = BOT / ".dispatch_frozen"
QUARANTINE = BOT / ".fleet_quarantine"
CAPFILE = BOT / ".fleet_cap"
SLOTS = BOT / ".fleet_slots"
USAGE_DB = BOT / "zai_usage.db"
LEDGER = BOT / "fleet_interventions.jsonl"
LKG = HERMES / "scripts" / "fleet_lkg.py"
FLEET_CFG = BOT / "fleet.json"
FREEZE_POLICY = BOT / "freeze_policy.json"
DEFAULT_FREEZE_TTL_S = 1800

ACTIONS = ("notify", "throttle", "freeze", "drain", "restart", "quarantine", "rollback",
           "restart-gateway", "resync-kanban", "re-elect-responder", "restart-fips",
           "reap-stale", "repair-state-db")
LEVEL = {"notify": 1, "throttle": 2, "freeze": 3, "drain": 4, "restart": 5,
         "quarantine": 6, "rollback": 7,
         "resync-kanban": 2, "re-elect-responder": 2, "restart-gateway": 5,
         "restart-fips": 5, "reap-stale": 4, "repair-state-db": 5}


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _touch(p: Path, payload: dict | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        p.touch(exist_ok=True)
    else:
        p.write_text(json.dumps(payload, indent=2) + "\n")


def _ledger(entry: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _notify(reason: str) -> bool:
    try:
        c = sqlite3.connect(str(USAGE_DB), timeout=5)
        c.execute("INSERT INTO anomaly_events (ts, severity, category, title, detail) "
                  "VALUES (?,?,?,?,?)",
                  (time.time(), "warning", "fleet-remediate",
                   "fleet peer/self remediation", reason))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def _freeze_ttl_s() -> int:
    """Freeze lifetime (seconds) from the version-controlled policy, 0 = sticky.

    A freeze is an ephemeral runaway brake, not a permanent state: without a TTL
    a hard load/cpu spike that has since cleared leaves `.dispatch_frozen`
    latched (the 2026-09-21 stale-freeze incident). `fleet_freeze_guard.py`
    honours `expires_at`.
    """
    try:
        ttl = int((_read_json(FREEZE_POLICY, {}) or {}).get("ttl_s", DEFAULT_FREEZE_TTL_S))
    except (TypeError, ValueError):
        ttl = DEFAULT_FREEZE_TTL_S
    return max(0, ttl)


def act_freeze(reason: str, ttl_s: int | None = None) -> str:
    if ttl_s is None:
        ttl_s = _freeze_ttl_s()
    now_ts = time.time()
    now = datetime.now(timezone.utc).isoformat()
    payload = {"engaged_at": now, "reason": f"fleet-remediate: {reason}",
               "ttl_s": ttl_s,
               "expires_at": (now_ts + ttl_s) if ttl_s > 0 else None}
    _touch(ESTOP, payload)
    _touch(FROZEN)
    return f"frozen (ESTOP + .dispatch_frozen; ttl={ttl_s or 'sticky'}s)"


def act_quarantine(reason: str) -> str:
    act_freeze(reason)
    _touch(QUARANTINE, {"engaged_at": datetime.now(timezone.utc).isoformat(),
                        "reason": f"fleet-remediate: {reason}"})
    return "quarantined (ESTOP + .dispatch_frozen + .fleet_quarantine)"


def act_throttle(reason: str) -> str:
    _touch(CAPFILE, {"cap": 1, "reason": reason,
                     "ts": datetime.now(timezone.utc).isoformat()})
    return "throttled (admission cap=1)"


def _worker_pids() -> list[tuple[int, int]]:
    out = []
    if not SLOTS.is_dir():
        return out
    for f in SLOTS.glob("*.slot"):
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            f.unlink(missing_ok=True)
            continue
        except PermissionError:
            pass
        try:
            etimes = int(Path(f"/proc/{pid}/stat").read_text().split()[21])  # jiffies since boot
        except Exception:
            etimes = 0
        out.append((pid, etimes))
    return out


def act_drain(reason: str) -> str:
    # Drain always freezes first so the killed slots are not immediately refilled.
    act_freeze(reason)
    cap = 4
    cfg = _read_json(FLEET_CFG, {})
    try:
        cap = int((_read_json(CAPFILE, {}) or {}).get("cap")
                  or (cfg.get("caps", {}) or {}).get("max_workers", {}).get(
                      cfg.get("node", ""), 4) or 4)
    except Exception:
        pass
    procs = _worker_pids()
    if len(procs) <= cap:
        return f"drain: {len(procs)}<={cap}, nothing to kill"
    procs.sort(key=lambda x: x[1], reverse=True)   # oldest first
    excess = procs[cap:]
    killed = 0
    for pid, _ in excess:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
    return f"drain: TERM {killed} worker(s), kept {cap}"


def act_restart(reason: str, allow_gateway: bool = False) -> str:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    done = []
    services = ["zai-proxy.service"] + (["hermes-gateway.service"] if allow_gateway else [])
    for svc in services:
        try:
            r = subprocess.run(["systemctl", "--user", "restart", svc], env=env,
                               capture_output=True, text=True, timeout=90)
            done.append(f"{svc}={'ok' if r.returncode == 0 else 'fail'}")
        except Exception as exc:  # noqa: BLE001
            done.append(f"{svc}=error:{exc}")
    return "restart " + ", ".join(done)


def act_rollback(reason: str) -> str:
    if not Path(LKG).exists():
        return "rollback: fleet_lkg.py missing"
    idx = _read_json(BOT / "fleet_lkg" / "index.json", {"snapshots": []})
    goods = [s for s in idx.get("snapshots", []) if s.get("status") == "good"]
    if not goods:
        return "rollback: no good snapshot"
    ref = sorted(goods, key=lambda s: s.get("ts", 0))[-1]["id"]
    try:
        r = subprocess.run([sys.executable, str(LKG), "restore", ref, "--yes"],
                           capture_output=True, text=True, timeout=180)
        return f"rollback to {ref}: rc={r.returncode} {r.stdout.strip()[:120]}"
    except Exception as exc:  # noqa: BLE001
        return f"rollback error: {exc}"


def act_resync_kanban(reason: str) -> str:
    script = HERMES / "scripts" / "kanban_git.py"
    if not script.exists():
        return "resync-kanban: kanban_git.py missing"
    env = dict(os.environ)
    # Bulk board commits hang on the legacy class-pattern scan (~6.8k task files).
    env["HERMES_LEGACY_PATTERN_SCAN"] = "off"
    try:
        r = subprocess.run([sys.executable, str(script), "sync"],
                           capture_output=True, text=True, timeout=600, env=env)
        head = ""
        try:
            rr = subprocess.run(["git", "-C", os.path.expanduser("~/hermes-kanban"),
                                 "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10)
            head = rr.stdout.strip()
        except Exception:
            pass
        _write_json(BOT / "kanban_git_state.json",
                    {"ts": time.time(), "rc": r.returncode, "head": head})
        return f"resync-kanban rc={r.returncode} {r.stdout.strip()[:100]}"
    except Exception as exc:  # noqa: BLE001
        return f"resync-kanban error: {exc}"


def act_re_elect_responder(reason: str) -> str:
    p = BOT / "buzz_responder_state.json"
    st = _read_json(p, {}) or {}
    st["force_election_ts"] = time.time()
    st.pop("leader", None)
    _write_json(p, st)
    return "re-elect-responder: forced a fresh election"


def act_restart_fips(reason: str) -> str:
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart",
                            "fips", "fips-dns", "fips-firewall"],
                           capture_output=True, text=True, timeout=120)
        return f"restart-fips rc={r.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"restart-fips error: {exc}"


def act_restart_gateway(reason: str) -> str:
    if _worker_pids():
        return "restart-gateway: deferred (workers running)"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(["systemctl", "--user", "restart", "hermes-gateway.service"],
                           env=env, capture_output=True, text=True, timeout=90)
        return f"restart-gateway rc={r.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"restart-gateway error: {exc}"


def act_repair_state_db(reason: str) -> str:
    """Loss-aware offline state.db recovery (see state_db_autorepair.py).

    The helper is bounded and idempotent; when it decides a recovery is partial
    it pages instead of installing, so this verb can never silently drop data.
    """
    script = HERMES / "scripts" / "state_db_autorepair.py"
    if not script.exists():
        return "repair-state-db: state_db_autorepair.py missing"
    try:
        r = subprocess.run([sys.executable, str(script), "--apply", "--json"],
                           capture_output=True, text=True, timeout=1500)
        tail = (r.stdout or "").strip().replace("\n", " ")[-300:]
        return f"repair-state-db rc={r.returncode} {tail}"
    except Exception as exc:  # noqa: BLE001
        return f"repair-state-db error: {exc}"


def act_reap_stale(reason: str) -> str:
    before = len(list(SLOTS.glob("*.slot"))) if SLOTS.is_dir() else 0
    _worker_pids()  # prunes dead slot files as a side effect
    after = len(list(SLOTS.glob("*.slot"))) if SLOTS.is_dir() else 0
    return f"reap-stale: pruned {before - after} stale slot(s)"


def peer_ssh(node: str, action: str, reason: str, request_id: str) -> list[str]:
    """Build the SSH argv to run this remediator on a peer (for arbiters)."""
    cfg = _read_json(FLEET_CFG, {})
    peer = next((p for p in cfg.get("peers", []) if p.get("name") == node), None)
    if not peer:
        return []
    hosts = peer.get("hosts") or ([peer["host"]] if peer.get("host") else [])
    host = hosts[0] if hosts else None
    if not host:
        return []
    key = os.path.expanduser(peer.get("key", "~/.ssh/id_dq05"))
    user = peer.get("user", "c03rad0r")
    remote_script = f"{HERMES}/scripts/fleet_remediate.py"
    remote = (f"python3 {remote_script} {action} "
              f"--reason {json.dumps(reason)} --request-id {request_id}")
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=8", "-i", key, f"{user}@{host}", remote]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet remediation ladder")
    ap.add_argument("action", choices=ACTIONS + ("status",))
    ap.add_argument("--reason", default="unspecified")
    ap.add_argument("--request-id", default="")
    ap.add_argument("--allow-gateway", action="store_true")
    args = ap.parse_args(argv)

    if args.action == "status":
        print(json.dumps({
            "frozen": ESTOP.exists() or FROZEN.exists(),
            "quarantined": QUARANTINE.exists(),
            "cap_file": _read_json(CAPFILE, None),
            "workers": len(_worker_pids()),
        }, indent=1))
        return 0

    reason = args.reason
    if args.action == "notify":
        result = f"notified={_notify(reason)}"
    elif args.action == "freeze":
        result = act_freeze(reason)
    elif args.action == "quarantine":
        result = act_quarantine(reason)
    elif args.action == "throttle":
        result = act_throttle(reason)
    elif args.action == "drain":
        result = act_drain(reason)
    elif args.action == "restart":
        result = act_restart(reason, args.allow_gateway)
    elif args.action == "rollback":
        result = act_rollback(reason)
    elif args.action == "restart-gateway":
        result = act_restart_gateway(reason)
    elif args.action == "resync-kanban":
        result = act_resync_kanban(reason)
    elif args.action == "re-elect-responder":
        result = act_re_elect_responder(reason)
    elif args.action == "restart-fips":
        result = act_restart_fips(reason)
    elif args.action == "reap-stale":
        result = act_reap_stale(reason)
    elif args.action == "repair-state-db":
        result = act_repair_state_db(reason)
    else:  # pragma: no cover
        result = "unknown"

    entry = {"ts": time.time(), "action": args.action, "level": LEVEL[args.action],
             "reason": reason, "request_id": args.request_id, "result": result,
             "node": _read_json(FLEET_CFG, {}).get("node", "?")}
    _ledger(entry)
    print(f"[fleet-remediate] {args.action} (L{LEVEL[args.action]}): {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
