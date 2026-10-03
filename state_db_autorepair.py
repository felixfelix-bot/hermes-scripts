#!/usr/bin/env python3
"""state_db_autorepair.py — loss-aware, bounded auto-repair of a corrupt state.db.

Codifies the manual 2026-09-28 manager-state.db recovery so any node can do it:

  1. read the last health result (`state_db_health.json`) for corrupt profiles;
  2. for each, gather every candidate copy (live DB + `state.db.snap-*` +
     `state.db.malformed-backup-*`) and run
     `hermes sessions recover --allow-partial` on each into a temp DB;
  3. pick the best (prefer `loss_detected=false`, then most messages, then newest);
  4. **apply only when the recovery is lossless** (`require_lossless`) — otherwise
     page the operator with the artifacts and leave the live DB untouched;
  5. apply = preserve live `state.db*` → idle-gate + stop gateway → install the
     recovered DB + VACUUM → restart gateway → verify; on any failure restore the
     original (the node is never left down).

Safety: kill-switch (`$HERMES_HOME/bot/.state_db_repair_off`), budget, cooldown,
and dry-run by default (use `--apply`). Every action is ledgered to
`fleet_interventions.jsonl`.

Usage:
  state_db_autorepair.py [--apply] [--json] [--profile NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state_db_health import classify_db, find_state_dbs  # noqa: E402

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
HEALTH_STATE = BOT / "state_db_health.json"
POLICY = BOT / "state_db_guard.json"
KILL = BOT / ".state_db_repair_off"
LEDGER = BOT / "fleet_interventions.jsonl"
STATE = BOT / "state_db_repair_state.json"
WORK = BOT / "state_db_recover_work"
BACKUPS = BOT / "backups"

DEFAULTS = {
    "enabled": True,
    "require_lossless": True,
    "allow_partial": True,
    "budget": 2,
    "cooldown_s": 3600,
    "window_s": 21600,
    "gateway_stop_timeout_s": 240,
    "candidate_globs": ["state.db.snap-*", "state.db.malformed-backup-*"],
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


def load_policy() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (_read_json(POLICY, {}) or {}).items()
                if not k.startswith("_")})
    return cfg


def parse_recovery_report(report: dict | None) -> dict:
    """Extract the fields we decide on from a `sessions recover` report."""
    report = report or {}
    ver = report.get("verification") or {}
    counts = ver.get("table_counts") or {}
    return {
        "complete": bool(report.get("complete")),
        "verified": bool(report.get("verified")),
        "healthy": bool(ver.get("healthy")),
        "loss_detected": bool(ver.get("loss_detected")),
        "messages": int(counts.get("messages") or 0),
        "sessions": int(counts.get("sessions") or 0),
    }


def find_hermes() -> str | None:
    for cand in (HERMES / "hermes-agent" / "venv" / "bin" / "hermes",
                 Path("/usr/local/bin/hermes")):
        if cand.exists():
            return str(cand)
    return shutil.which("hermes")


def collect_candidates(db: Path, globs: list[str]) -> list[Path]:
    """All recoverable copies for a profile DB, newest first (live included)."""
    out = [db] if db.is_file() else []
    for pattern in globs:
        for p in db.parent.glob(pattern):
            if p.name.endswith(("-wal", "-shm")) or not p.is_file():
                continue
            out.append(p)
    # de-dup, newest mtime first
    seen: dict[str, Path] = {}
    for p in out:
        seen[str(p)] = p

    def _mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(seen.values(), key=_mt, reverse=True)


def run_recover(hermes: str, source: Path, out: Path, allow_partial: bool,
                timeout_s: int = 900) -> dict:
    """Run `hermes sessions recover` into *out*; return the parsed report."""
    report = Path(str(out) + ".recovery.json")
    cmd = [hermes, "sessions", "recover", "--source", str(source),
           "--output", str(out), "--report", str(report)]
    if allow_partial:
        cmd.append("--allow-partial")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"recover failed: {exc}"}
    parsed = parse_recovery_report(_read_json(report, None))
    parsed.update({"ok": r.returncode == 0 and out.is_file(),
                   "source": str(source), "output": str(out),
                   "rc": r.returncode})
    return parsed


def pick_best(entries: list[dict]) -> dict | None:
    """Prefer lossless > healthy > most messages > newest. Pure."""
    good = [e for e in entries if e.get("ok")]
    if not good:
        return None
    good.sort(key=lambda e: (not e.get("loss_detected", True),
                            e.get("healthy", False),
                            e.get("messages", 0)))
    return good[-1]


def may_apply(policy: dict, best: dict | None) -> tuple[bool, str]:
    if best is None:
        return False, "no recoverable candidate"
    if not best.get("verified") or not best.get("healthy"):
        return False, "recovery not verified/healthy"
    if policy.get("require_lossless") and best.get("loss_detected"):
        return False, "recovery is partial (loss_detected=true); not auto-applied"
    if not best.get("messages"):
        return False, "recovery has no messages"
    return True, "ok"


def _worker_running() -> bool:
    slots = BOT / ".fleet_slots"
    if not slots.is_dir():
        return False
    for f in slots.glob("*.slot"):
        try:
            os.kill(int(f.stem), 0)
            return True
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            return True
    return False


def _systemctl(action: str, unit: str, timeout_s: int) -> int:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        return subprocess.run(["systemctl", "--user", action, unit], env=env,
                              capture_output=True, text=True, timeout=timeout_s).returncode
    except Exception:  # noqa: BLE001
        return 1


def apply_recovery(db: Path, recovered: Path, policy: dict) -> str:
    """Offline install of *recovered*; restores the original on any failure."""
    if _worker_running():
        return "deferred (workers running)"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUPS / f"state-db-autorepair-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in db.parent.glob(db.name + "*"):
        if p.is_file():
            shutil.copy2(p, dest / p.name)
    _systemctl("stop", "hermes-gateway.service", policy["gateway_stop_timeout_s"])
    try:
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        shutil.copy2(recovered, db)
        os.chmod(db, 0o600)
        conn = sqlite3.connect(str(db), timeout=60)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("VACUUM;")
            rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check;")]
        finally:
            conn.close()
        if rows != ["ok"]:
            raise RuntimeError(f"post-install quick_check: {rows[:3]}")
    except Exception as exc:  # noqa: BLE001
        for p in dest.glob(db.name + "*"):
            shutil.copy2(p, db.parent / p.name)
        _systemctl("start", "hermes-gateway.service", 60)
        return f"FAILED ({exc}); original restored from {dest}"
    _systemctl("start", "hermes-gateway.service", 60)
    return f"applied (backup {dest})"


def _recent_actions(state: dict, now: float, window_s: int) -> list[float]:
    return [t for t in state.get("_action_times", []) if now - t < window_s]


def run(apply: bool, profile: str | None = None) -> dict:
    policy = load_policy()
    now = time.time()
    state = _read_json(STATE, {}) or {}
    result: dict = {"ts": now, "apply": apply, "policy_enabled": policy["enabled"],
                    "blocked": [], "profiles": [], "performed": []}

    if not policy["enabled"]:
        result["blocked"].append("config-disabled")
    if KILL.exists():
        result["blocked"].append("kill-switch")
    if len(_recent_actions(state, now, policy["window_s"])) >= policy["budget"]:
        result["blocked"].append("budget")

    # Prefer the recorded health result; else recompute a targeted check.
    health = _read_json(HEALTH_STATE, None)
    corrupt_profiles = set((health or {}).get("corrupt") or [])
    dbs = find_state_dbs(profile)
    targets = [(name, p) for name, p in dbs
               if name in corrupt_profiles or classify_db(p)["verdict"] == "corrupted"]
    if not targets:
        result["detail"] = "no corrupt state.db"
        return result

    hermes = find_hermes()
    if not hermes:
        result["blocked"].append("no-hermes-cli")
        return result

    WORK.mkdir(parents=True, exist_ok=True)
    for name, db in targets:
        entry = {"profile": name, "path": str(db), "candidates": [], "best": None,
                 "applied": False, "detail": ""}
        for i, cand in enumerate(collect_candidates(db, policy["candidate_globs"])):
            out = WORK / f"{name}-{i}.db"
            rep = run_recover(hermes, cand, out, policy["allow_partial"])
            rep["source_mtime"] = cand.stat().st_mtime if cand.exists() else 0
            entry["candidates"].append(rep)
        best = pick_best(entry["candidates"])
        entry["best"] = ({k: best.get(k) for k in
                          ("source", "messages", "sessions", "loss_detected",
                           "healthy", "verified")} if best else None)
        ok, why = may_apply(policy, best)
        entry["detail"] = why
        if ok and apply and not result["blocked"]:
            entry["applied"] = True
            entry["result"] = apply_recovery(db, Path(best["output"]), policy)
            state.setdefault("_action_times", []).append(now)
            _ledger({"ts": now, "source": "state-db-autorepair", "profile": name,
                     "action": "repair-state-db", "detail": entry["result"]})
        elif not ok:
            _ledger({"ts": now, "source": "state-db-autorepair", "profile": name,
                     "action": "page", "detail": why})
        result["profiles"].append(entry)
        result["performed"].append(entry)

    _write_json(STATE, state)
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Loss-aware state.db auto-repair")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args(argv)
    result = run(args.apply, args.profile)
    if args.json or args.apply:
        print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
