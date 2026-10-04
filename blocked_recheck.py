#!/usr/bin/env python3
"""blocked_recheck.py — release completion-pending cards when their gate clears.

gate_tick parks a done-but-ungated card as status='blocked', block_kind=
'completion' (distinct from an operator block). Those cards can only come back
when the missing evidence exists — e.g. a cold cross-family review becomes
possible after a provider outage, or an operator answers a decision. Nothing
currently re-checks them, so the chain stalls until a human notices.

This sweeper re-evaluates every completion-pending card against the gates and
flips it back to 'done' (clearing block_kind) when it passes. Idempotent, no-op
when nothing changes.

Usage:
  blocked_recheck.py [--board B ...] [--report]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "governance"))
sys.path.insert(0, str(HERE))
import gate_engine as ge  # type: ignore


def _boards(slugs):
    if slugs:
        return [ge.BOARDS / s for s in slugs]
    return sorted(p.parent for p in ge.BOARDS.glob("*/kanban.db"))


def _cols(conn):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    except sqlite3.Error:
        return set()


def _comment(conn, tid, body):
    conn.execute(
        "insert into task_comments (task_id, author, body, created_at) "
        "values (?,?,?,?)", (tid, "blocked-recheck", body, int(time.time())))


def recheck(slugs, gates, report=False):
    released = []
    for bdir in _boards(slugs):
        db = bdir / "kanban.db"
        if not db.exists():
            continue
        conn = sqlite3.connect(str(db))
        try:
            if "block_kind" not in _cols(conn):
                continue
            rows = conn.execute(
                "select id from tasks where status='blocked' "
                "and coalesce(block_kind,'')='completion'").fetchall()
            if not rows:
                continue
            for (tid,) in rows:
                res = ge.evaluate_task(bdir.name, tid, gates)
                verdict = res.get("verdict")
                if verdict in ("pass", "grandfathered", "waived"):
                    if not report:
                        _comment(conn, tid, "blocked-recheck: completion gates "
                                            "now satisfied — releasing to done.")
                        conn.execute("update tasks set status='done', "
                                     "block_kind=NULL where id=?", (tid,))
                        conn.commit()
                    released.append({"board": bdir.name, "id": tid,
                                     "verdict": verdict})
        finally:
            conn.close()
    return released


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="append", dest="boards")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    gates = ge.load_gates()
    rel = recheck(args.boards, gates, report=args.report)
    if rel:
        for r in rel:
            print(f"[blocked-recheck] released {r['board']}/{r['id']} "
                  f"(verdict={r['verdict']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
