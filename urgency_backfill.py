#!/usr/bin/env python3
"""Phase N: migrate every board to the urgency schema and backfill open work.

- Adds `urgency` + `urgency_deadline` + `urgency_set_at` + `urgency_source`
  columns and the `(status, urgency)` index to every board (idempotent).
- Backfills NULL urgency on OPEN work (`ready/running/todo/scheduled/blocked`)
  to `soon` with `urgency_source='backfill'`.
- Leaves `done`/`archived`/`cancelled` rows NULL (historical records).

Usage:
    urgency_backfill.py [--boards-root DIR] [--dry-run] [--default soon]
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import time

OPEN_STATUSES = ("ready", "running", "todo", "scheduled", "blocked")
COLS = [
    ("urgency", "TEXT CHECK (urgency IN ('now','soon','defer','batch'))"),
    ("urgency_deadline", "INTEGER"),
    ("urgency_set_at", "INTEGER"),
    ("urgency_source", "TEXT"),
]


def migrate_db(db: str, default: str, dry_run: bool) -> tuple[list, int]:
    conn = sqlite3.connect(db, timeout=15)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        added = []
        if "urgency" not in have:
            for name, decl in COLS:
                if name not in have:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
                    added.append(name)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_urgency "
                "ON tasks(status, urgency)")
        now = int(time.time())
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        n_null = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE urgency IS NULL "
            f"AND status IN ({placeholders})", OPEN_STATUSES).fetchone()[0]
        if dry_run:
            conn.rollback()
            return added, n_null
        cur = conn.execute(
            f"UPDATE tasks SET urgency=?, urgency_source='backfill', "
            f"urgency_set_at=? WHERE urgency IS NULL AND status IN ({placeholders})",
            (default, now, *OPEN_STATUSES))
        conn.commit()
        return added, cur.rowcount
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards-root",
                    default=os.path.expanduser("~/.hermes/kanban/boards"))
    ap.add_argument("--default", default="soon")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dbs = sorted(glob.glob(os.path.join(args.boards_root, "*", "kanban.db")))
    tot_added = tot_backfilled = boards_touched = misses = 0
    for db in dbs:
        board = os.path.basename(os.path.dirname(db))
        try:
            added, n = migrate_db(db, args.default, args.dry_run)
            if added:
                tot_added += 1
            if n:
                boards_touched += 1
                tot_backfilled += n
            if added or n:
                verb = "would add" if args.dry_run else "added"
                bverb = "would backfill" if args.dry_run else "backfilled"
                print(f"[{board}] {verb}={added or '-'} {bverb}={n}")
        except Exception as e:
            misses += 1
            print(f"[{board}] ERROR {e} — skipped (fail-open)")
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"\n{mode}: boards={len(dbs)} with_columns_added={tot_added} "
          f"boards_backfilled={boards_touched} rows_backfilled={tot_backfilled} "
          f"errors={misses}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
