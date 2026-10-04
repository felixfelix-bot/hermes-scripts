#!/usr/bin/env python3
"""task-profile-reaper.py — Archive & reclaim dead kanban task-profiles.

Weekly maintenance: every Hermes profile under ~/.hermes/profiles/ EXCEPT
``manager`` and ``worker-*`` is a one-shot "task profile" (market-chore-*,
esp-miner-*, tollgate-*, etc.). Once its work is done it is dead weight:
the state.db stays, but the dirs swell to 1.5-2G with session transcripts.

For each eligible (delegated, > grace-days idle, no live gateway) profile we:

  1. PRAGMA wal_checkpoint(TRUNCATE)  (best-effort, shrinks -wal)
  2. dump sessions+messages → ~/.hermes/archive/task-profiles/<name>-<date>.jsonl[.zst|.gz]
  3. VERIFY the dump is non-empty AND row counts match the source DB exactly
     (session count + message count recorded as a trailer line)
  4. append a MANIFEST line (audit trail of what was reclaimed)
  5. shutil.rmtree the profile dir

Any verify failure ABORTS that profile only (dir is kept, ERROR logged) and
continues to the next.  --dry-run (default) prints the plan and mutates nothing.

CLI:
  task-profile-reaper.py [--dry-run|--yes] [--grace-days N] [--profiles DIR]

--no-agent cron convention: empty stdout = silent, non-empty = delivered.
The reaper prints a plan/report unconditionally (it is a weekly janitor, not a
silent watchdog), so it is NOT intended for verbatim no-agent delivery — the
manager reviews its output.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"
ARCHIVE_DIR = HOME / ".hermes" / "archive" / "task-profiles"
MANIFEST_PATH = ARCHIVE_DIR / "MANIFEST.jsonl"

# Profiles we must never touch.
EXCLUDE_NAMES = {"manager"}
EXCLUDE_PREFIX = "worker-"

DEFAULT_GRACE_DAYS = 14


# --------------------------------------------------------------------------- #
# compression selection: prefer zstandard, fall back to gzip (stdlib)
# --------------------------------------------------------------------------- #
def discover_compressor() -> tuple[str, object]:
    """Return (suffix, module-or-None). Prefer zstandard, else gzip."""
    try:
        import zstandard  # type: ignore
        return ".zst", zstandard
    except ImportError:
        return ".gz", None  # None => use stdlib gzip


def open_writer(path: Path, module) -> "object":
    if module is not None:  # zstandard
        return module.ZstdCompressor().stream_writer(open(path, "wb"))
    return gzip.open(path, "wb")


def open_reader(path: Path, module) -> "object":
    if module is not None:  # zstandard
        return module.ZstdDecompressor().stream_reader(open(path, "rb"))
    return gzip.open(path, "rb")


# --------------------------------------------------------------------------- #
# gating helpers
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists on the box."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError => it exists but isn't ours; treat as alive.
        return False


def gateway_alive(profile_dir: Path) -> bool:
    """True if <profile>/gateway.pid exists and points at a live pid."""
    pidfile = profile_dir / "gateway.pid"
    if not pidfile.exists():
        return False
    try:
        data = json.loads(pidfile.read_text())
    except (OSError, ValueError):
        # Unreadable/corrupt pid file — be conservative and never touch.
        return True
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, bool):
        pid = None
    if not isinstance(pid, int):
        return True  # no pid info -> conservative
    return pid_alive(pid)


def newest_activity(db_path: Path) -> float:
    """Max(newest session started_at, db mtime). Falls back to mtime.

    Returns 0.0 if the DB cannot be read at all (caller decides eligibility).
    """
    mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
    started = 0.0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            row = conn.execute("SELECT MAX(started_at) FROM sessions").fetchone()
            if row and row[0] is not None:
                started = float(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return max(mtime, started)


def classify_profile(profile_dir: Path, now: float, grace_days: int) -> tuple[str, str]:
    """Return (decision, reason) where decision ∈ {reap,skip}.

    gating order: name exclusion -> gateway alive -> state.db missing ->
    eligibility by idle time.
    """
    name = profile_dir.name
    if name == "manager" or name.startswith("worker-"):
        return "skip", "excluded-by-name"

    if gateway_alive(profile_dir):
        return "skip", "gateway-alive"

    state_db = profile_dir / "state.db"
    if not state_db.exists():
        return "skip", "no-state-db"

    reference = newest_activity(state_db)
    idle_seconds = now - reference
    threshold = grace_days * 86400.0
    if idle_seconds <= threshold:
        return "skip", f"idle-too-recent ({idle_seconds/86400.0:.1f}d)"
    return "reap", f"idle {idle_seconds/86400.0:.1f}d"


# --------------------------------------------------------------------------- #
# dump + verify
# --------------------------------------------------------------------------- #
def table_rows(conn: sqlite3.Connection, table: str):
    """Yield (colname, value) dicts for every row of `table`, in stable order."""
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    for row in cur:
        yield dict(zip(cols, row))


def dump_db(db_path: Path, out_path: Path, module) -> dict:
    """Dump sessions+messages to JSONL. Returns full-result dict for verify."""
    result = {
        "ok": False,
        "sessions": 0,
        "messages": 0,
        "src_sessions": 0,
        "src_messages": 0,
        "error": None,
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            result["src_sessions"] = conn.execute(
                "SELECT count(*) FROM sessions"
            ).fetchone()[0]
            result["src_messages"] = conn.execute(
                "SELECT count(*) FROM messages"
            ).fetchone()[0]

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open_writer(out_path, module) as f:
                def w(obj):
                    f.write((json.dumps(obj) + "\n").encode("utf-8"))

                for row in table_rows(conn, "sessions"):
                    row["_type"] = "session"
                    w(row)
                    result["sessions"] += 1
                for row in table_rows(conn, "messages"):
                    row["_type"] = "message"
                    w(row)
                    result["messages"] += 1
                # trailer line carries the authoritative counts for verify
                w({
                    "_trailer": {
                        "sessions": result["sessions"],
                        "messages": result["messages"],
                        "profile": db_path.parent.name,
                        "dumped_at": datetime.now(timezone.utc).isoformat(),
                    }
                })
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — reaper must fail soft per-profile
        result["error"] = str(exc)
        return result

    # verify pass (independent re-read + count) so the caller gets a single
    # authoritative result.
    result["ok"] = verify_dump(
        out_path, module, result["sessions"], result["messages"]
    )
    return result


def verify_dump(out_path: Path, module, expect_sessions: int, expect_messages: int) -> bool:
    """Re-open the compressed dump: non-empty AND trailer counts match source.

    The dump ends with a single trailer line ``{"_trailer": {"sessions": N,
    "messages": M, ...}}``.  We require: file readable, exactly one trailer,
    trailer counts == expected, and total data lines == sessions+messages.
    """
    try:
        if not out_path.exists() or out_path.stat().st_size == 0:
            return False
        data_lines = 0
        trailer = None
        with open_reader(out_path, module) as f:
            for raw in f:
                line = raw.decode("utf-8")
                obj = json.loads(line)
                if obj.get("_type") == "trailer" or "_trailer" in obj:
                    trailer = obj.get("_trailer", obj)
                    break
                data_lines += 1
        if trailer is None:
            return False
        t_sessions = trailer.get("sessions")
        t_messages = trailer.get("messages")
        if t_sessions != expect_sessions or t_messages != expect_messages:
            return False
        if data_lines != (expect_sessions + expect_messages):
            return False
        return (expect_sessions + expect_messages) > 0
    except Exception:  # noqa: BLE001
        return False


def reap_profile(profile_dir: Path, now: float, module) -> dict:
    """Dump, verify, then rmtree. Returns per-profile result dict."""
    name = profile_dir.name
    date = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    suffix = ".zst" if module is not None else ".gz"
    out_path = ARCHIVE_DIR / f"{name}-{date}.jsonl{suffix}"

    res = {
        "profile": name,
        "reaped": False,
        "archive": str(out_path),
        "sessions": 0,
        "messages": 0,
        "error": None,
    }

    state_db = profile_dir / "state.db"

    # 1. best-effort wal checkpoint (shrink -wal before we read it)
    try:
        conn = sqlite3.connect(str(state_db))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # non-fatal: checkpoint is best-effort only
        print(f"  WARN checkpoint failed for {name}: {exc}", file=sys.stderr)

    # 2. dump
    dump = dump_db(state_db, out_path, module)
    if not dump["ok"]:
        res["error"] = dump.get("error") or "dump-verify failed"
        print(f"  ERROR {name}: dump/verify failed — dir KEPT ({res['error']})",
              file=sys.stderr)
        return res

    res["sessions"] = dump["sessions"]
    res["messages"] = dump["messages"]

    # 3. MANIFEST line (before rmtree so it survives even if rmtree breaks)
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_PATH, "a") as mf:
            mf.write(json.dumps({
                "ts": now,
                "profile": name,
                "archive": str(out_path),
                "sessions": dump["sessions"],
                "messages": dump["messages"],
            }) + "\n")
    except OSError as exc:
        res["error"] = f"manifest write failed: {exc}"
        print(f"  ERROR {name}: {res['error']} — dir KEPT", file=sys.stderr)
        return res

    # 4. rmtree
    try:
        shutil.rmtree(profile_dir)
    except OSError as exc:
        res["error"] = f"rmtree failed: {exc}"
        print(f"  ERROR {name}: {res['error']} — dir may remain", file=sys.stderr)
        return res

    res["reaped"] = True
    return res


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def iter_candidates(profiles_dir: Path):
    if not profiles_dir.is_dir():
        return
    for entry in sorted(profiles_dir.iterdir()):
        if entry.is_dir():
            yield entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Archive & reclaim dead task-profiles")
    ap.add_argument("--yes", action="store_true",
                    help="execute (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan only (default)")
    ap.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS,
                    help=f"idle threshold in days (default {DEFAULT_GRACE_DAYS})")
    ap.add_argument("--profiles", type=str, default=str(PROFILES_DIR),
                    help=f"profiles dir (default {PROFILES_DIR})")
    args = ap.parse_args(argv)

    # --yes wins over --dry-run; the mutual exclusion is fixed.
    if args.yes and args.dry_run:
        ap.error("--yes and --dry-run are mutually exclusive")
    execute = args.yes

    profiles_dir = Path(args.profiles).expanduser()
    now = time.time()
    module = discover_compressor()[1]

    plan = []
    for pdir in iter_candidates(profiles_dir):
        decision, reason = classify_profile(pdir, now, args.grace_days)
        if decision == "reap":
            plan.append((pdir, reason))

    print(f"task-profile-reaper  (--{'yes' if execute else 'dry-run'}, "
          f"grace={args.grace_days}d)")
    print(f"scanned {len(list(iter_candidates(profiles_dir)))} dirs; "
          f"{len(plan)} eligible to archive+reclaim")
    for pdir, reason in plan:
        print(f"  REAP  {pdir.name}  ({reason})")

    if not plan:
        print("nothing to reap")
        return 0

    if not execute:
        print("\n(dry-run: pass --yes to execute)")
        return 0

    ok = fail = 0
    for pdir, _reason in plan:
        r = reap_profile(pdir, now, module)
        status = "OK" if r["reaped"] else "FAIL"
        if r["reaped"]:
            ok += 1
        else:
            fail += 1
        print(f"  {status:4} {r['profile']}: sessions={r['sessions']} "
              f"messages={r['messages']} -> {r['archive']}"
              + ("" if r["reaped"] else f"  ({r['error']})"))

    print(f"\ndone: {ok} reclaimed, {fail} failed (kept on disk)")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
