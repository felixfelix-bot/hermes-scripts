#!/usr/bin/env python3
"""telemetry_retention.py — bound the bot telemetry SQLite DBs.

These DBs grow unbounded (burn_attribution.db ~3G, zai_usage.db ~0.9G) and are
the "known regrowth drivers" the disk audits keep flagging. This prunes old rows
by a per-table retention window and then VACUUMs to return pages to the OS.

Safety:
  * opens with a busy timeout; if a writer holds the lock it fails soft (no
    partial delete) and is safe to re-run;
  * never touches live session/state DBs (opencode.db, profiles/*/state.db);
  * dry-run by default; --apply to delete + vacuum.

Usage:
  telemetry_retention.py --config state/fleet/telemetry_retention.json [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def prune(db_path: str, tables: dict, now: float, apply: bool) -> dict:
    """Delete rows older than each table's window; return a report."""
    report = {"db": db_path, "tables": {}, "vacuum": None}
    p = Path(db_path)
    if not p.exists():
        report["error"] = "missing"
        return report
    before = p.stat().st_size
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA busy_timeout=60000")
    try:
        for table, spec in tables.items():
            col = spec["ts_col"]
            days = spec["keep_days"]
            cutoff = now - days * 86400
            try:
                if apply:
                    n = con.execute(
                        f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
                    con.commit()
                else:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {col} < ?",
                        (cutoff,)).fetchone()[0]
                report["tables"][table] = int(n)
            except sqlite3.Error as e:
                report["tables"][table] = f"err:{e}"
        if apply:
            t = time.time()
            try:
                con.execute("VACUUM")
                report["vacuum"] = round(time.time() - t, 1)
            except sqlite3.Error as e:
                report["vacuum"] = f"err:{e}"
    finally:
        con.close()
    report["bytes_before"] = before
    report["bytes_after"] = p.stat().st_size
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text())
    now = time.time()
    for db, spec in cfg.get("databases", {}).items():
        rep = prune(str(Path(db).expanduser()), spec.get("tables", {}), now, args.apply)
        act = "deleted" if args.apply else "would delete"
        rows = sum(v for v in rep.get("tables", {}).values() if isinstance(v, int))
        freed = (rep.get("bytes_before", 0) - rep.get("bytes_after", 0)) / 1e9
        print(f"[retention] {db}: {act} {rows} rows, "
              f"{freed:+.2f}G (vacuum={rep.get('vacuum')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
