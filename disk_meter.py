#!/usr/bin/env python3
"""disk_meter.py — measure real disk footprints so placement can reserve space
(Phase V6.0).

The governor needs a *measured* per-task disk delta (not a guess) to decide where
disk-heavy work runs. This records two things into ``~/.hermes/state/disk.db``:

  * ``samples`` — periodic free/used for a path (the curve),
  * ``run_disk`` — the delta around one task run (the placement signal).

Pure functions are unit-tested (tests/test_disk_meter.py). CLI:
  disk_meter.py --once             # sample the path once + record
  disk_meter.py --report [--json]  # counts + per-host totals
  disk_meter.py --suggest [--json] # per-class reserve suggestion from deltas
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE = HERMES / "state"
DB = STATE / "disk.db"
DEFAULT_PATH = os.environ.get("DISK_METER_PATH", "/")


# ── pure helpers ──────────────────────────────────────────────────────────────
def du_to_gb(text: str) -> float:
    """Parse `du -sk <path>` (KB) output to GB. 0.0 on anything unparseable."""
    for tok in (text or "").split():
        try:
            return round(int(tok) / 1024 ** 2, 3)
        except ValueError:
            continue
    return 0.0


def delta_gb(free_before: int, free_after: int) -> float:
    """GB consumed between two free-space readings (negative = space freed)."""
    return round((free_before - free_after) / 1e9, 3)


def free_used(path: str) -> tuple[int, int, int]:
    du = shutil.disk_usage(path)
    return du.free, du.total, du.used


def suggest_reserve(deltas_gb: list[float], margin: float = 2.0,
                    floor_gb: int = 5) -> int:
    """Reserve suggestion: p90 of positive deltas × margin, floored.

    p90 ignores one-off outliers (a single huge build shouldn't reserve the
    disk), while the floor keeps a minimum headroom.
    """
    ds = sorted(d for d in deltas_gb if d > 0)
    if not ds:
        return int(floor_gb)
    p90 = ds[int(0.9 * (len(ds) - 1))]
    return int(max(floor_gb, math.ceil(p90 * margin)))


def workspace_gb(workspace: str) -> float:
    try:
        out = subprocess.run(["du", "-sk", str(workspace)], capture_output=True,
                             text=True, timeout=60).stdout
        return du_to_gb(out.split("\t", 1)[0])
    except Exception:
        return 0.0


# ── db ────────────────────────────────────────────────────────────────────────
def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS samples "
                     "(ts REAL, host TEXT, path TEXT, free INTEGER, total INTEGER, used INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS run_disk "
                     "(ts REAL, host TEXT, task_id TEXT, gb_used REAL, class TEXT)")
        conn.commit()
    finally:
        conn.close()


def record_sample(db_path: Path, host: str, path: str, free: int, total: int,
                  ts: float | None = None) -> None:
    init_db(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)",
                     (ts or time.time(), host, path, free, total, total - free))
        conn.commit()
    finally:
        conn.close()


def record_run_delta(db_path: Path, host: str, task_id: str, gb_used: float,
                     cls: str = "", ts: float | None = None) -> None:
    init_db(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("INSERT INTO run_disk VALUES (?,?,?,?,?)",
                     (ts or time.time(), host, task_id, gb_used, cls))
        conn.commit()
    finally:
        conn.close()


def load_run_deltas(db_path: Path, cls: str | None = None) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    try:
        q = "SELECT ts, host, task_id, gb_used, class FROM run_disk"
        args: tuple = ()
        if cls:
            q += " WHERE class=?"
            args = (cls,)
        return [{"ts": r[0], "host": r[1], "task_id": r[2], "gb_used": r[3], "class": r[4]}
                for r in conn.execute(q + " ORDER BY ts DESC", args)]
    finally:
        conn.close()


def report(db_path: Path, window_days: float = 7, now: float | None = None) -> dict:
    now = now or time.time()
    since = now - window_days * 86400
    if not db_path.exists():
        return {"samples": 0, "runs": 0, "hosts": {}}
    conn = _connect(db_path)
    try:
        samples = conn.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (since,)).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM run_disk WHERE ts>=?", (since,)).fetchone()[0]
        hosts: dict[str, dict] = {}
        for h, s in conn.execute("SELECT host, COUNT(*) FROM samples WHERE ts>=? GROUP BY host", (since,)):
            hosts.setdefault(h, {})["samples"] = s
        for h, r in conn.execute("SELECT host, COUNT(*) FROM run_disk WHERE ts>=? GROUP BY host", (since,)):
            hosts.setdefault(h, {})["runs"] = r
    finally:
        conn.close()
    return {"samples": samples, "runs": runs, "hosts": hosts}


def suggest_from_db(db_path: Path, margin: float = 2.0, floor_gb: int = 5) -> dict:
    deltas = [r["gb_used"] for r in load_run_deltas(db_path)]
    return {"reserve_gb": suggest_reserve(deltas, margin, floor_gb),
            "n_runs": len(deltas),
            "max_gb": round(max(deltas), 2) if deltas else 0.0}


def seed_history(db_path: Path, kanban_db: Path) -> dict:
    """Seed per-class disk priors from existing kanban tasks (Phase V6.0d).

    Records each surviving task workspace's `du` as an *upper-bound prior* tagged
    with the task's resource class. Boards generally don't store disk deltas, so
    this is priors only — the live capture supplies the real signal.
    """
    kdb = Path(kanban_db)
    if not kdb.exists():
        return {"seeded": 0, "reason": "no kanban db"}
    try:
        conn = sqlite3.connect(f"file:{kdb}?mode=ro", uri=True)
        rows = conn.execute("SELECT title, body, workspace_path FROM tasks").fetchall()
    except sqlite3.Error:
        return {"seeded": 0, "reason": "unreadable"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    n = 0
    for title, body, wp in rows:
        if not wp or not Path(wp).is_dir():
            continue
        try:
            import fleet_queue as fq  # type: ignore
            cls = fq.classify("", "", title or "", body or "")["resource_class"]
        except Exception:  # noqa: BLE001
            cls = ""
        record_run_delta(db_path, host="history", task_id=f"hist:{Path(wp).name}",
                         gb_used=workspace_gb(str(wp)), cls=cls)
        n += 1
    return {"seeded": n}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Disk footprint meter (Phase V6.0)")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--host", default=os.uname().nodename)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--suggest", action="store_true")
    ap.add_argument("--seed-history", action="store_true")
    ap.add_argument("--kanban-db", default=str(HERMES / "kanban.db"))
    ap.add_argument("--margin", type=float, default=2.0)
    ap.add_argument("--floor-gb", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = Path(args.db)

    if args.seed_history:
        s = seed_history(db, Path(args.kanban_db))
        print(json.dumps(s) if args.json else f"seeded {s['seeded']} priors from history")
        return 0
    if args.once:
        free, total, _ = free_used(args.path)
        record_sample(db, args.host, args.path, free, total)
        print(f"recorded sample {args.path}: {free/1e9:.1f}G free -> {db}")
        return 0
    if args.suggest:
        s = suggest_from_db(db, args.margin, args.floor_gb)
        print(json.dumps(s) if args.json else
              f"suggested reserve: {s['reserve_gb']}G (n={s['n_runs']}, max={s['max_gb']}G)")
        return 0
    rep = report(db)
    print(json.dumps(rep) if args.json else
          f"disk meter: {rep['samples']} samples, {rep['runs']} run deltas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
