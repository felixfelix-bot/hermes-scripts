#!/usr/bin/env python3
"""fleet_resource_collector.py — sample node resources into resource_metrics (K.8).

Feeds multi_resource_kalman.py by writing one row per run into
``~/.hermes/bot/zai_usage.db`` table ``resource_metrics`` with the columns it
reads: ``ts, cpu_load_1m, memory_used_percent, worker_count``. Creates the table
if missing and prunes rows older than the retention window.

Pure stdlib. Run every 5 min via systemd timer.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
DB = BOT / "zai_usage.db"
BOT_FIT = BOT / "fleet_fit.json"
RETAIN_H = 72


def _load1() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        return 0.0


def _mem_used_pct() -> float:
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            info[k.strip()] = int(rest.split()[0])
    except Exception:
        return 0.0
    total = info.get("MemTotal", 0) or 1
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (total - avail) / total, 2)


def _metrics() -> dict:
    """Collect a superset of columns both kalman schemas understand."""
    import shutil
    load1 = load5 = load15 = 0.0
    try:
        parts = Path("/proc/loadavg").read_text().split()
        load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        pass
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            info[k.strip()] = int(rest.split()[0])
    except Exception:
        pass
    total = info.get("MemTotal", 0) or 1
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    try:
        du = shutil.disk_usage(str(HERMES))
        disk_used_pct = int(round(100.0 * du.used / du.total)) if du.total else 0
        disk_avail = du.free
    except Exception:
        disk_used_pct, disk_avail = 0, 0
    return {
        "ts": int(time.time()),
        "cpu_load_1m": round(load1, 2),
        "cpu_load_5m": round(load5, 2),
        "cpu_load_15m": round(load15, 2),
        "memory_available_mb": avail // 1024,
        "memory_used_percent": int(round(100.0 * (total - avail) / total)),
        "swap_used_percent": int(round(100.0 * swap_used / swap_total)) if swap_total else 0,
        "swap_used_kb": swap_used,
        "swap_total_kb": swap_total,
        "disk_used_percent": disk_used_pct,
        "disk_avail_bytes": disk_avail,
        "worker_count": _workers(),
    }


_RICH_SCHEMA = [
    ("cpu_load_1m", "REAL"), ("cpu_load_5m", "REAL"), ("cpu_load_15m", "REAL"),
    ("memory_available_mb", "INTEGER"), ("memory_used_percent", "INTEGER"),
    ("swap_used_percent", "INTEGER"), ("swap_used_kb", "INTEGER"),
    ("swap_total_kb", "INTEGER"), ("disk_used_percent", "INTEGER"),
    ("disk_avail_bytes", "INTEGER"), ("worker_count", "INTEGER"),
]


def _workers() -> int:
    slots = BOT / ".fleet_slots"
    n = 0
    if slots.is_dir():
        for f in slots.glob("*.slot"):
            try:
                os.kill(int(f.stem), 0)
                n += 1
            except (ProcessLookupError, ValueError):
                continue
            except PermissionError:
                n += 1
        if n:
            return n
    try:
        out = subprocess.run(["pgrep", "-fc", " -p worker"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out or 0)
    except Exception:
        return 0


def collect() -> int:
    DB.parent.mkdir(parents=True, exist_ok=True)
    m = _metrics()
    conn = sqlite3.connect(str(DB), timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS resource_metrics (ts INTEGER PRIMARY KEY)")
        have = {r[1] for r in conn.execute("PRAGMA table_info(resource_metrics)")}
        # Migrate/align to the rich superset schema (additive only).
        for col, typ in _RICH_SCHEMA:
            if col not in have:
                conn.execute(f"ALTER TABLE resource_metrics ADD COLUMN {col} {typ}")
        cols = [c for c, _ in _RICH_SCHEMA] + ["ts"]
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO resource_metrics ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(m.get(c) for c in cols))
        conn.execute("DELETE FROM resource_metrics WHERE ts < ?",
                     (m["ts"] - RETAIN_H * 3600,))
        conn.commit()
    finally:
        conn.close()
    return 0


def main() -> int:
    collect()
    print(f"[resource-collector] {Path.home().name} "
          f"load1={_load1()} mem={_mem_used_pct()}% workers={_workers()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
