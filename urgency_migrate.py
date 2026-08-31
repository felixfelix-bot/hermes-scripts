#!/usr/bin/env python3
"""CG-11: add urgency columns to every kanban board. Idempotent."""
import glob, os, sqlite3, time, sys

ROOT = os.path.expanduser("~/.hermes/kanban/boards")
COLS = [
    ("urgency",          "TEXT CHECK (urgency IN ('now','soon','defer','batch'))"),
    ("urgency_deadline", "INTEGER"),
    ("urgency_set_at",   "INTEGER"),
    ("urgency_source",   "TEXT"),
]

def migrate(db):
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    added = []
    for name, decl in COLS:
        if name not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
            added.append(name)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(status, urgency)")
    # Backfill: mid-flight tasks -> soon (no deadline, no escalation for backfilled rows)
    cur = conn.execute(
        "UPDATE tasks SET urgency='soon', urgency_source='backfill', "
        "urgency_set_at=? WHERE urgency IS NULL AND status IN ('ready','running')",
        (int(time.time()),))
    backfilled = cur.rowcount
    conn.commit()
    reclassify = list(conn.execute(
        "SELECT id, title, status FROM tasks WHERE urgency IS NULL "
        "AND status IN ('todo','scheduled','blocked')"))
    conn.close()
    return added, backfilled, reclassify

if __name__ == "__main__":
    for db in sorted(glob.glob(os.path.join(ROOT, "*", "kanban.db"))):
        board = os.path.basename(os.path.dirname(db))
        try:
            added, n, rec = migrate(db)
            print(f"[{board}] added={added or '-'} backfilled_soon={n}")
            for tid, title, st in rec:
                print(f"  RECLASSIFY {tid} ({st}): {title[:60]}")
        except Exception as e:
            print(f"[{board}] ERROR {e} — skipped (fail-open)", file=sys.stderr)
