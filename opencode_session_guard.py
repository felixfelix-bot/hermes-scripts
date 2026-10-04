#!/usr/bin/env python3
"""opencode_session_guard.py — warn before resuming an old interactive session.

Resuming a long-lived session with `opencode -s <id>` re-attaches its entire
retained history, which floods the context window and makes the client compact
constantly (see docs/SESSION-HYGIENE.md). This guard reads the opencode DB,
reports the age of a session you are about to resume, and refuses (exit 3) when
it is older than `--max-age-days`.

Usage:
  opencode_session_guard.py --session <id> [--max-age-days 2] [--db PATH]
  opencode_session_guard.py --max-age-days 2 -- opencode -s <id> [-m ...]
  # with `-- cmd...`: the age check runs, then cmd is exec'd (only if it passes)

Exit: 0 = safe/unknown; 3 = refusing an old resume.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/share/opencode/opencode.db"


def age_days(created_ms: int | None, now_ms: int | None = None) -> float | None:
    """Age of a session in days, or None when the timestamp is unknown."""
    if not created_ms:
        return None
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now_ms - created_ms) / 86_400_000.0


def lookup_created_ms(db: Path, session_id: str) -> int | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT time_created FROM session WHERE id=?",
                          (session_id,)).fetchone()
        con.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def check(db: Path, session_id: str | None, max_age_days: float,
          now_ms: int | None = None) -> tuple[bool, str]:
    """Return (allowed, message). A missing session/timestamp is allowed."""
    if not session_id:
        return True, "no -s/--session given (fresh session) — OK"
    created = lookup_created_ms(db, session_id)
    d = age_days(created, now_ms)
    if d is None:
        return True, f"session {session_id}: age unknown (treated as OK)"
    if d > max_age_days:
        return False, (f"REFUSING to resume {session_id}: {d:.1f} days old "
                       f"(> {max_age_days:g}). Start a fresh session "
                       f"(docs/SESSION-HYGIENE.md).")
    return True, f"session {session_id}: {d:.2f} days old — OK"


def _extract_session(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a in ("-s", "--session") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--session="):
            return a.split("=", 1)[1]
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--max-age-days", type=float, default=2.0)
    ap.add_argument("--session", default=None)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--check-only", action="store_true",
                    help="only report (exit 3 on refuse); never exec a command")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="after `--`: command to exec once the check passes")
    args = ap.parse_args(argv)

    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    sid = args.session or _extract_session(cmd)
    allowed, msg = check(Path(args.db), sid, args.max_age_days, args.now_ms)
    print(f"[session-guard] {msg}", file=sys.stderr)
    if not allowed:
        return 3
    if cmd and not args.check_only:
        os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
