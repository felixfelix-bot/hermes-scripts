#!/usr/bin/env python3
"""worker-session-hygiene.py — daily per-worker session-store maintenance.

For each ``worker-*`` Hermes profile:

  1. Close stale OPEN sessions: any session with ``ended_at IS NULL``,
     ``source IN ('subagent','cli')``, ``started_at`` older than 7 days, and
     no message newer than 7 days → mark ended with ``end_reason='reaped_stale'``.
     (The ``NOT IN`` recency subquery uses ``messages.timestamp``; a session
     with no messages at all is stale by construction and qualifies.)

  2. ``hermes sessions prune --older-than 30 --yes``  (ended-only, safe).

  3. IF the gateway is dead: ``hermes sessions optimize`` — gated on
     ``PRAGMA freelist_count > 1000`` so we don't VACUUM a DB that isn't
     meaningfully bloated.

Safety constraints (honoured strictly):
  * ``manager`` and anything not ``worker-*`` is NEVER touched.
  * The close-stale UPDATE is single-row-in-effect and safe under WAL — it
    runs regardless of gateway state (it is the sanctioned "no CLI close"
    path).
  * prune (DELETE) and optimize (VACUUM) run ONLY when ``gateway.pid`` is
    dead — never while a live gateway holds the DB.

Output: one summary line per profile. Exit 0 unless a profile's close-stale
UPDATE itself failed unexpectedly (CLI prune/optimize failures are logged as
warnings and don't alone flip the exit code — see NOTES below).

NOTES: worker state.db files on this box have orphaned FTS5 shadow tables
(``messages_fts_data`` present without the ``messages_fts`` vtable), so the
hermes CLI cannot open them and ``sessions prune``/``optimize`` fail with
"error creating shadow table". Those need ``hermes sessions repair`` first —
a separate, manager-reviewed action this script deliberately does NOT auto-run.

--no-agent cron convention: empty stdout = silent; the hygiene janitor prints
a per-profile summary unconditionally, so it is NOT intended for verbatim
no-agent delivery — the manager reviews its output.
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
from pathlib import Path

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"

STALE_SECONDS = 7 * 86400
PRUNE_DAYS = 30
OPTIMIZE_FREELIST_MIN = 1000

_STALE_WHERE = (
    "ended_at IS NULL "
    "AND source IN ('subagent','cli') "
    "AND started_at < ? "
    "AND id NOT IN ("
    "  SELECT session_id FROM messages WHERE timestamp > ?"
    ")"
)


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def stale_close_sql(now: float) -> tuple[str, tuple[float, float]]:
    """The close-stale UPDATE. Returns (sql, params) for test clarity."""
    params = (now - STALE_SECONDS, now - STALE_SECONDS)
    return (
        "UPDATE sessions SET ended_at=strftime('%s','now'), "
        "end_reason='reaped_stale' WHERE " + _STALE_WHERE,
        params,
    )


def stale_candidates_count(conn: sqlite3.Connection, now: float) -> int:
    """How many sessions the close-stale UPDATE would touch (dry-run)."""
    params = (now - STALE_SECONDS, now - STALE_SECONDS)
    row = conn.execute(
        "SELECT count(*) FROM sessions WHERE " + _STALE_WHERE, params
    ).fetchone()
    return int(row[0]) if row else 0


def stale_close(conn: sqlite3.Connection, now: float) -> int:
    """Execute the close-stale UPDATE and return rows affected."""
    sql, params = stale_close_sql(now)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def freelist_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA freelist_count").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# --------------------------------------------------------------------------- #
# gating / process helpers
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def gateway_alive(profile_dir: Path) -> bool:
    pidfile = profile_dir / "gateway.pid"
    if not pidfile.exists():
        return False
    try:
        data = json.loads(pidfile.read_text())
    except (OSError, ValueError):
        return True  # unreadable -> conservative
    pid = data.get("pid") if isinstance(data, dict) else None
    if not isinstance(pid, int):
        return True  # no pid info -> conservative
    return pid_alive(pid)


def hermes_bin() -> str | None:
    candidates = [
        HOME / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
        HOME / ".local" / "bin" / "hermes",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("hermes")


def run_hermes(profile_dir: Path, args: list[str]) -> tuple[int, str]:
    """Run `hermes <args>` with HERMES_HOME=<profile>. Returns (rc, output)."""
    binpath = hermes_bin()
    if binpath is None:
        return 127, "hermes binary not found"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_dir)
    try:
        proc = subprocess.run(
            [binpath, *args], env=env, capture_output=True, text=True, timeout=600
        )
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode, out
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


# --------------------------------------------------------------------------- #
# per-profile processing
# --------------------------------------------------------------------------- #
def process_profile(profile_dir: Path, now: float, dry_run: bool) -> dict:
    name = profile_dir.name
    summary = {
        "profile": name,
        "closed": 0,
        "prune": "skipped",
        "optimize": "skipped",
        "error": None,
    }
    state_db = profile_dir / "state.db"
    if not state_db.exists():
        # no session store to maintain — skip, not an error
        summary["prune"] = "no-db"
        summary["optimize"] = "no-db"
        return summary

    alive = gateway_alive(profile_dir)

    try:
        conn = sqlite3.connect(str(state_db))
        try:
            if dry_run:
                # count only
                summary["closed"] = stale_candidates_count(conn, now)
                summary["prune"] = "would-prune" if not alive else "gateway-alive"
                summary["optimize"] = (
                    "would-optimize" if (not alive and freelist_count(conn) > OPTIMIZE_FREELIST_MIN)
                    else "skip-freelist" if not alive else "gateway-alive"
                )
                conn.close()
                return summary

            summary["closed"] = stale_close(conn, now)
            summary["freelist"] = freelist_count(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        summary["error"] = f"sqlite: {exc}"
        return summary

    # prune: ended-only DELETE, safe — but only when gateway dead
    if not alive:
        rc, out = run_hermes(profile_dir, ["sessions", "prune",
                                           "--older-than", str(PRUNE_DAYS), "--yes"])
        summary["prune"] = "ok" if rc == 0 else f"fail(rc={rc}): {out[:120]}"
    else:
        summary["prune"] = "gateway-alive"

    # optimize: VACUUM — gateway dead AND freelist-gated
    if not alive and summary.get("freelist", 0) > OPTIMIZE_FREELIST_MIN:
        rc, out = run_hermes(profile_dir, ["sessions", "optimize"])
        summary["optimize"] = "ok" if rc == 0 else f"fail(rc={rc}): {out[:120]}"
    elif not alive:
        summary["optimize"] = "skip-freelist"
    else:
        summary["optimize"] = "gateway-alive"

    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily worker session-store hygiene")
    ap.add_argument("--dry-run", action="store_true",
                    help="count stale sessions, do not mutate")
    ap.add_argument("--yes", action="store_true", help="execute (default)")
    ap.add_argument("--profiles", type=str, default=str(PROFILES_DIR))
    args = ap.parse_args(argv)
    dry_run = args.dry_run

    profiles_dir = Path(args.profiles).expanduser()
    now = time.time()

    workers = sorted(
        p for p in profiles_dir.iterdir()
        if p.is_dir() and p.name.startswith("worker-")
    ) if profiles_dir.is_dir() else []

    mode = "dry-run" if dry_run else "execute"
    print(f"worker-session-hygiene  ({mode}, stale>{STALE_SECONDS // 86400}d, "
          f"prune>{PRUNE_DAYS}d)")

    errored = 0
    total_closed = 0
    for pdir in workers:
        s = process_profile(pdir, now, dry_run)
        total_closed += s["closed"]
        if s["error"]:
            errored += 1
        err = f"  ERROR={s['error']}" if s["error"] else ""
        print(f"  {s['profile']}: closed={s['closed']} prune={s['prune']} "
              f"optimize={s['optimize']}{err}")

    print(f"done: {len(workers)} workers, {total_closed} stale sessions closed"
          + (" (dry-run: counted only)" if dry_run else ""))
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
