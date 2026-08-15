#!/usr/bin/env python3
"""reap_stale_sessions — close never-ended sessions left behind by crashes.

Hermes profiles accumulate sessions with ``ended_at IS NULL`` when processes
die without a clean shutdown (crashes, kills, lost SSH). They bloat state.db
and pollute "active session" views. This reaper closes them the same way
hermes-agent itself would (hermes_state.end_session: first end_reason wins,
resume reopens by clearing ended_at) so the operation is non-destructive and
reversible:

  stale  := ended_at IS NULL
            AND last_activity < cutoff          (--cutoff YYYY-MM-DD, UTC)
            AND no live compression_locks row   (expires_at > now)
  close  := UPDATE sessions SET ended_at=now, end_reason='reaped_stale'
            WHERE id=? AND ended_at IS NULL

Every execute run writes a JSON snapshot of the exact prior rows BEFORE
mutating; ``--rollback <snap> --execute`` restores them (only rows still
carrying our marker, so a session someone else legitimately ended afterwards
is never clobbered).

Dry-run is the default; ``--execute`` is required for any mutation.
Zero tokens — pure sqlite.

Usage:
  # inspect (no changes):
  python3 reap_stale_sessions.py --db ~/.hermes/profiles/worker-layout/state.db --cutoff 2026-08-12
  # sweep every profile:
  python3 reap_stale_sessions.py --all-profiles --cutoff 2026-08-12
  # close them, with rollback snapshots:
  python3 reap_stale_sessions.py --all-profiles --cutoff 2026-08-12 \
      --execute --snapshot-dir ~/.hermes/reap_snapshots
  # undo:
  python3 reap_stale_sessions.py --rollback ~/.hermes/reap_snapshots/<file>.json --execute

  hermes cron create --no-agent --script reap_stale_sessions.py \
      --name reap-stale-sessions --deliver local "15 4 * * *"
  (cron args: --all-profiles --cutoff <yesterday's date> --execute
   --snapshot-dir ~/.hermes/reap_snapshots)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

END_REASON = "reaped_stale"
TOOL = "reap_stale_sessions"
VERSION = 1
DEFAULT_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"


def parse_cutoff(text: str) -> float:
    """YYYY-MM-DD (UTC) -> epoch seconds. Refuses future dates."""
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    epoch = dt.timestamp()
    if epoch > time.time():
        raise ValueError(f"cutoff {text} is in the future; refusing to reap live sessions")
    return epoch


def find_stale(con: sqlite3.Connection, cutoff: float, now: float):
    """Rows eligible for reaping: never ended, inactive before cutoff, no live lock."""
    return con.execute(
        """
        SELECT s.id, s.source, s.title, s.started_at, s.ended_at, s.end_reason,
               COALESCE((SELECT MAX(m.timestamp) FROM messages m
                         WHERE m.session_id = s.id), s.started_at) AS last_activity
        FROM sessions s
        WHERE s.ended_at IS NULL
          AND COALESCE((SELECT MAX(m.timestamp) FROM messages m
                        WHERE m.session_id = s.id), s.started_at) < ?
          AND NOT EXISTS (SELECT 1 FROM compression_locks l
                          WHERE l.session_id = s.id AND l.expires_at > ?)
        ORDER BY last_activity
        """,
        (cutoff, now),
    ).fetchall()


def snapshot_path(snapshot_dir: Path, label: str, now: float) -> Path:
    ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return snapshot_dir / f"{label}_{ts}.json"


def reap_db(db_path: Path, cutoff: float, execute: bool,
            snapshot_dir: Path | None, label: str | None = None) -> tuple[int, int]:
    """Reap one state.db. Returns (reaped, skipped_locked).

    skipped_locked counts sessions that were stale by time but held a live
    compression lock (already excluded by find_stale; kept 0 for clarity).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = find_stale(con, cutoff, time.time())
    con.close()
    label = label or db_path.parent.name

    print(f"[{label}] db={db_path}")
    if not rows:
        print(f"[{label}] reaped=0 skipped=0 (no stale never-ended sessions)")
        return (0, 0)

    mode = "EXECUTE" if execute else "DRY-RUN (pass --execute to apply)"
    print(f"[{label}] {len(rows)} stale session(s) older than cutoff — {mode}:")
    for r in rows:
        sid, source, title, started, _ended, _reason, last_act = r[:7]
        when = datetime.fromtimestamp(last_act, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {sid}  src={source}  last_activity={when}  title={(title or '')[:40]!r}")

    if not execute:
        print(f"[{label}] reaped=0 skipped=0 (dry-run)")
        return (0, 0)

    snapshot_dir = snapshot_dir or (Path.home() / ".hermes" / "reap_snapshots")
    now = time.time()
    snap = {
        "tool": TOOL,
        "version": VERSION,
        "db": str(db_path.resolve()),
        "cutoff": cutoff,
        "end_reason": END_REASON,
        "created_at": now,
        "rows": [
            {"id": r[0], "source": r[1], "title": r[2], "started_at": r[3],
             "ended_at": r[4], "end_reason": r[5], "last_activity": r[6]}
            for r in rows
        ],
    }
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snapshot_path(snapshot_dir, label, now)
    snap_file.write_text(json.dumps(snap, indent=2))
    print(f"[{label}] snapshot -> {snap_file}")

    con = sqlite3.connect(db_path, timeout=30)
    try:
        reap_times = {}
        with con:
            for r in rows:
                sid = r[0]
                cur = con.execute(
                    "UPDATE sessions SET ended_at = ?, end_reason = ? "
                    "WHERE id = ? AND ended_at IS NULL "
                    "AND COALESCE((SELECT MAX(m.timestamp) FROM messages m "
                    "              WHERE m.session_id = sessions.id), "
                    "             started_at) < ?",
                    (now, END_REASON, sid, cutoff),
                )
                reap_times[sid] = now if cur.rowcount else None
        # record what we actually set so rollback can match exactly
        snap["rows"] = [dict(row, reap_ended_at=reap_times.get(row["id"]))
                        for row in snap["rows"]]
        snap_file.write_text(json.dumps(snap, indent=2))
    finally:
        con.close()

    applied = sum(1 for v in reap_times.values() if v is not None)
    print(f"[{label}] reaped={applied} skipped={len(rows) - applied}")
    return (applied, len(rows) - applied)


def rollback(snapshot_file: Path, execute: bool) -> int:
    """Restore rows captured in a snapshot. Only rows still marked reaped_stale
    with the exact reap ended_at are cleared — newer legitimate ends win."""
    snap = json.loads(Path(snapshot_file).read_text())
    db_path = Path(snap["db"])
    rows = snap.get("rows", [])
    print(f"[rollback] snapshot={snapshot_file} db={db_path} rows={len(rows)}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    candidates = []
    for row in rows:
        cur = con.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id = ?", (row["id"],)
        ).fetchone()
        if cur and cur[1] == END_REASON and cur[0] == row.get("reap_ended_at"):
            candidates.append(row["id"])
        else:
            print(f"  skip {row['id']} (no longer matches snapshot: {cur})")
    con.close()

    if not execute:
        print(f"[rollback] DRY-RUN: would restore {len(candidates)} session(s) "
              f"(pass --execute to apply)")
        return 0

    con = sqlite3.connect(db_path, timeout=30)
    try:
        with con:
            for sid in candidates:
                con.execute(
                    "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                    "WHERE id = ? AND end_reason = ? AND ended_at IS NOT NULL",
                    (sid, END_REASON),
                )
    finally:
        con.close()
    print(f"[rollback] restored={len(candidates)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="Close stale never-ended Hermes sessions (dry-run by default).",
    )
    ap.add_argument("--db", type=Path, help="path to a single state.db")
    ap.add_argument("--profiles-root", type=Path, default=DEFAULT_PROFILES_ROOT,
                    help=f"profiles dir (default: {DEFAULT_PROFILES_ROOT})")
    ap.add_argument("--all-profiles", action="store_true",
                    help="sweep <profiles-root>/*/state.db")
    ap.add_argument("--cutoff", help="YYYY-MM-DD (UTC); sessions with no "
                    "activity before this are stale (not needed for --rollback)")
    ap.add_argument("--execute", action="store_true",
                    help="apply changes (default: dry-run)")
    ap.add_argument("--snapshot-dir", type=Path,
                    default=Path.home() / ".hermes" / "reap_snapshots",
                    help="where rollback snapshots are written on --execute")
    ap.add_argument("--rollback", type=Path, metavar="SNAPSHOT.json",
                    help="restore a prior execute run (needs --execute)")
    args = ap.parse_args(argv)

    if args.rollback:
        if not args.rollback.exists():
            print(f"error: snapshot not found: {args.rollback}", file=sys.stderr)
            return 2
        return rollback(args.rollback, args.execute)

    if not args.db and not args.all_profiles:
        print("error: pass --db <state.db> or --all-profiles "
              "(implicit sweeps of the real profiles dir are refused)",
              file=sys.stderr)
        return 2

    if not args.cutoff:
        print("error: --cutoff is required for reap mode", file=sys.stderr)
        return 2

    try:
        cutoff = parse_cutoff(args.cutoff)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.db:
        if not args.db.exists():
            print(f"error: db not found: {args.db}", file=sys.stderr)
            return 2
        targets = [(args.db.parent.name, args.db)]
    else:
        root = args.profiles_root
        if not root.is_dir():
            print(f"error: profiles root not found: {root}", file=sys.stderr)
            return 2
        targets = sorted((p.parent.name, p) for p in root.glob("*/state.db"))
        if not targets:
            print(f"error: no state.db under {root}", file=sys.stderr)
            return 2

    total = 0
    failures = 0
    for label, db_path in targets:
        try:
            n, _ = reap_db(db_path, cutoff, args.execute, args.snapshot_dir, label)
        except sqlite3.Error as e:
            failures += 1
            print(f"[{label}] ERROR: {e} (skipped — re-run is idempotent)",
                  file=sys.stderr)
            continue
        total += n
    if len(targets) > 1:
        msg = f"TOTAL reaped={total} across {len(targets)} profile(s)"
        if failures:
            msg += f" ({failures} errored)"
        print(msg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
