#!/usr/bin/env python3
"""triage_sweep.py — report the stalled kanban backlog (ADR: backlog burn-down).

The fleet burn-up gap never shrinks because a large pool of cards sits in
``triage``/``blocked`` (plus ``todo``) without progressing or being archived.
This tool is **report-only** by default: it lists the stalled cards older than a
threshold, grouped by board and status, so the operator can decide what to
resolve, re-dispatch, or archive. ``--apply`` is intentionally not implemented
yet (a later, operator-approved step).

Writes ``~/.hermes/logs/triage-sweep.md`` and prints a digest-friendly summary.

Usage:
  triage_sweep.py [--age-days 14] [--json] [--boards-dir DIR] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DEFAULT_BOARDS = HERMES / "kanban" / "boards"
DEFAULT_OUT = HERMES / "logs" / "triage-sweep.md"
STALLED = ("triage", "blocked", "todo")


def _to_epoch(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def classify(rows: list[dict], now: float, age_days: int) -> dict:
    """Pure: bucket by board+status for rows older than age_days (excl archived)."""
    cutoff = now - age_days * 86400
    by_board: Counter = Counter()
    by_status: Counter = Counter()
    oldest: list[dict] = []
    for r in rows:
        st = r.get("status")
        if st not in STALLED:
            continue
        ca = _to_epoch(r.get("created_at"))
        if ca is None:
            continue
        if ca > cutoff:
            continue
        by_board[r.get("board", "?")] += 1
        by_status[st] += 1
        oldest.append({"board": r.get("board"), "id": r.get("id"), "status": st,
                       "assignee": r.get("assignee"), "age_days": round((now - ca) / 86400, 1),
                       "title": (r.get("title") or "")[:60],
                       "block_kind": r.get("block_kind")})
    oldest.sort(key=lambda x: -x["age_days"])
    return {
        "age_days": age_days,
        "total_stalled": sum(by_board.values()),
        "by_status": dict(by_status.most_common()),
        "by_board": dict(by_board.most_common(20)),
        "oldest": oldest[:40],
    }


def scan(boards_dir: Path = DEFAULT_BOARDS) -> list[dict]:
    rows: list[dict] = []
    for db in sorted(boards_dir.glob("*/kanban.db")):
        board = db.parent.name
        if board.startswith(("_", ".")):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            for r in con.execute(
                    "select id,title,status,assignee,created_at,block_kind from tasks"):
                d = dict(r)
                d["board"] = board
                rows.append(d)
            con.close()
        except Exception:
            continue
    return rows


def render_md(v: dict, ts: str) -> str:
    lines = [f"# Triage sweep — {ts}", "",
             f"Stalled (triage/blocked/todo) older than {v['age_days']}d: "
             f"**{v['total_stalled']}**", ""]
    lines.append("## By status")
    for s, n in v["by_status"].items():
        lines.append(f"- {s}: {n}")
    lines.append("")
    lines.append("## By board (top 20)")
    for b, n in v["by_board"].items():
        lines.append(f"- {b}: {n}")
    lines.append("")
    lines.append("## Oldest cards")
    for o in v["oldest"][:25]:
        lines.append(f"- {o['board']}/{str(o['id'])[:12]} [{o['status']}] "
                     f"{o['age_days']}d — {o['title']}")
    lines.append("")
    lines.append("_Report-only. Decide per card: re-dispatch, reassign, or archive._")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--age-days", type=int, default=14)
    ap.add_argument("--boards-dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    boards = Path(os.path.expanduser(args.boards_dir)) if args.boards_dir else DEFAULT_BOARDS
    out = Path(os.path.expanduser(args.out)) if args.out else DEFAULT_OUT
    now = time.time()
    v = classify(scan(boards), now, args.age_days)
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(v, ts))

    if args.json:
        print(json.dumps(v, indent=2))
    else:
        print(f"triage-sweep: stalled>{args.age_days}d={v['total_stalled']} "
              f"| " + " ".join(f"{k}={n}" for k, n in v["by_status"].items())
              + f" | report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
