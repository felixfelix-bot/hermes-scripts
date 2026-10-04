#!/usr/bin/env python3
"""state_db_health.py — snapshot-based integrity probe for profile state.db files.

Canonical detector for the 2026-09-28 state.db-corruption incident. Absorbs the
*integrity* half of `scripts/engine/db_health_check.py` (that script keeps its
nightly compaction role) but runs on a fast timer (15 min) instead of nightly,
and never mutates a live database.

Design:
  * A live database is never poked under a running gateway. Small DBs
    (< SNAPSHOT_THRESHOLD_BYTES) are checked live with a short hang guard; at or
    above the threshold — or when a live check times out — a SQLite backup-API
    snapshot is taken and `quick_check` runs *offline* on the snapshot.
  * A timeout is reported as `unknown` (never `corrupted`), so a busy large DB
    cannot be mistaken for damage.
  * Writes one machine-readable result to `$HERMES_HOME/bot/state_db_health.json`;
    `fleet_component_health.probe_state_db()` reads it (cheap) and the component
    guard maps a `down` state_db to the `repair-state-db` verb.

Usage:
  state_db_health.py                 # probe all profiles, write state
  state_db_health.py --json          # also print the result
  state_db_health.py --profile manager
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
PROFILES_DIR = HERMES / "profiles"
STATE = BOT / "state_db_health.json"

SNAPSHOT_THRESHOLD_BYTES = int(
    os.environ.get("STATE_DB_SNAPSHOT_THRESHOLD_BYTES", 100 * 1024 * 1024))
LIVE_TIMEOUT_S = int(os.environ.get("STATE_DB_LIVE_TIMEOUT_S", 10))
SNAPSHOT_TIMEOUT_S = int(os.environ.get("STATE_DB_SNAPSHOT_TIMEOUT_S", 120))
PROBE_MAX_AGE_S = int(os.environ.get("STATE_DB_PROBE_MAX_AGE_S", 3600))
# Hard ceiling on the whole probe. On a busy node (e.g. dq05 running
# bitcoind/electrs) a single large DB's quick_check or snapshot can take tens of
# seconds; without a budget the probe wedges the timer and never writes state.
# Remaining DBs are reported 'unknown' (never 'corrupted') once the budget is
# spent.
TOTAL_BUDGET_S = int(os.environ.get("STATE_DB_PROBE_BUDGET_S", 300))
# Hard per-DB ceiling enforced in a child process, so a wedged SQLite C call
# (a backup blocked on a busy/locked source) can never hang the probe. The
# budget covers the sum; this covers any single DB.
PER_DB_TIMEOUT_S = int(os.environ.get("STATE_DB_PER_DB_TIMEOUT_S", 150))
# A hard per-DB timeout SIGKILLs the child, so its own cleanup cannot run. The
# parent therefore owns the snapshot temp path (passed via --tmp) and sweeps any
# leftover `.state_db_health-*` older than this many seconds. Without this the
# 2026-10 state-db incident leaked ~120 GiB of multi-GB snapshots on dq05.
STALE_TEMP_MAX_AGE_S = int(os.environ.get("STATE_DB_STALE_TEMP_MAX_AGE_S", 900))

POLICY = BOT / "state_db_guard.json"
# Long-lived profiles worth the per-tick quick_check scan by default. A node's
# per-task worktree profiles number in the dozens; scanning them every 15 min is
# pure waste (a full quick_check of the 220 MB manager alone is ~14 s). Operators
# widen this via probe_profiles in state_db_guard.json.
DEFAULT_PROBE_PROFILES = ["manager", "worker-admin", "worker-base", "worker-heavy",
                          "worker-reviewer"]


def _ts() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def selected(patterns: list[str], name: str) -> bool:
    """True if *name* matches the probe scope. Empty patterns = everything."""
    if not patterns:
        return True
    if name == "default":
        return True
    return any(p and p in name for p in patterns)


def _policy() -> dict:
    try:
        return json.loads(POLICY.read_text())
    except Exception:
        return {}


def find_state_dbs(
    profile: str | None = None, patterns: list[str] | None = None
) -> list[tuple[str, Path]]:
    """Return (profile, path) for the default DB and matching profile DBs.

    ``profile`` is a single substring filter (CLI); ``patterns`` scopes the
    multi-profile scan (policy). ``None`` uses DEFAULT_PROBE_PROFILES.
    """
    out: list[tuple[str, Path]] = []
    default = HERMES / "state.db"
    if default.is_file():
        out.append(("default", default))
    if PROFILES_DIR.is_dir():
        for sub in sorted(PROFILES_DIR.iterdir()):
            db = sub / "state.db"
            if sub.is_dir() and db.is_file():
                out.append((sub.name, db))
    if profile:
        return [pair for pair in out if profile in pair[0]]
    pats = DEFAULT_PROBE_PROFILES if patterns is None else patterns
    return [pair for pair in out if selected(pats, pair[0])]


def check_conn(conn: sqlite3.Connection, timeout_s: int) -> tuple[str, str]:
    """Run quick_check with a hang guard. Returns (verdict, detail).

    verdict is one of ``ok`` / ``corrupted`` / ``unknown`` (timeout only).
    """
    deadline = time.monotonic() + max(1, timeout_s)

    def _progress() -> int:
        return 1 if time.monotonic() > deadline else 0

    try:
        conn.set_progress_handler(_progress, 1000)
    except Exception:  # pragma: no cover - exotic builds
        pass
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check;")]
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            return "unknown", f"quick_check timeout after {timeout_s}s"
        return "corrupted", f"sqlite error: {exc}"
    except sqlite3.DatabaseError as exc:
        return "corrupted", f"sqlite error: {exc}"
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except Exception:  # pragma: no cover
            pass
    return verdict_from_rows(rows)


def verdict_from_rows(rows: list[str]) -> tuple[str, str]:
    """Pure classifier for quick_check output (unit-testable)."""
    if rows == ["ok"]:
        return "ok", "quick_check ok"
    return "corrupted", "; ".join(rows[:5]) or "quick_check returned no rows"


def _sweep_stale_temp(directory: Path, age_s: int = STALE_TEMP_MAX_AGE_S) -> int:
    """Remove orphaned `.state_db_health-*` snapshots older than ``age_s``.

    A SIGKILLed per-DB child cannot clean up after itself, so its multi-GB
    snapshot temp files linger. Sweeping here bounds the leak. Returns the count
    removed (best-effort; never raises).
    """
    removed = 0
    cutoff = time.time() - age_s
    try:
        for leftover in directory.glob(".state_db_health-*"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def classify_db(path: Path, threshold: int | None = None,
                tmp_path: Path | None = None) -> dict:
    """Classify one state.db without mutating it. Returns a result dict."""
    threshold = SNAPSHOT_THRESHOLD_BYTES if threshold is None else threshold
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"profile": path.parent.name, "path": str(path), "verdict": "unknown",
                "via": "stat", "size": 0, "detail": f"stat failed: {exc}"}
    if size < threshold:
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            try:
                verdict, detail = check_conn(conn, LIVE_TIMEOUT_S)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            verdict, detail = "corrupted", f"open failed: {exc}"
        via = "live"
    else:
        tmp = tmp_path or path.parent / f".state_db_health-{os.getpid()}-{int(_ts())}"
        try:
            snapshot_db(path, tmp, SNAPSHOT_TIMEOUT_S)
            conn = sqlite3.connect(str(tmp), timeout=5)
            try:
                verdict, detail = check_conn(conn, SNAPSHOT_TIMEOUT_S)
            finally:
                conn.close()
        except TimeoutError:
            verdict, detail = "unknown", f"snapshot timeout after {SNAPSHOT_TIMEOUT_S}s"
        except sqlite3.Error as exc:
            verdict, detail = "corrupted", f"snapshot failed: {exc}"
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)
        via = "snapshot"
    return {"profile": path.parent.name, "path": str(path), "verdict": verdict,
            "via": via, "size": size, "detail": detail}


def snapshot_db(src: Path, dst: Path, timeout_s: int | None = None) -> None:
    """Consistent copy via the SQLite backup API (WAL-safe, no checkpoint).

    With ``timeout_s`` the copy aborts (raising TimeoutError) once the deadline
    passes, so a busy/large source can't wedge the caller indefinitely.
    """
    deadline = (time.monotonic() + timeout_s) if timeout_s else None
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst), timeout=30)
        try:
            def _progress(status, remaining, total):  # noqa: ANN001
                if deadline and time.monotonic() > deadline:
                    return 0
                return remaining

            src_conn.backup(dst_conn, progress=_progress, sleep=0.05)
            if deadline and time.monotonic() > deadline:
                raise TimeoutError(f"snapshot exceeded {timeout_s}s")
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _classify_isolated(path: Path) -> dict:
    """Classify one DB in a child process with a hard timeout.

    A wedged SQLite C call (a backup blocked on a busy/locked source) can't be
    interrupted by a Python-level deadline, so the per-DB check runs
    out-of-process and is killed at PER_DB_TIMEOUT_S. Failure/timeout yields
    'unknown', never 'corrupted'.
    """
    tmp = path.parent / f".state_db_health-{os.getpid()}-{int(_ts())}"
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--check-one", str(path),
             "--tmp", str(tmp)],
            capture_output=True, text=True, timeout=PER_DB_TIMEOUT_S)
        out = (proc.stdout or "").strip().splitlines()
        if proc.returncode == 0 and out:
            return json.loads(out[-1])
        return {"profile": path.parent.name, "path": str(path), "verdict": "unknown",
                "via": "child", "size": 0, "detail": f"child rc={proc.returncode}"}
    except subprocess.TimeoutExpired:
        return {"profile": path.parent.name, "path": str(path), "verdict": "unknown",
                "via": "child", "size": 0,
                "detail": f"child timeout after {PER_DB_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001
        return {"profile": path.parent.name, "path": str(path), "verdict": "unknown",
                "via": "child", "size": 0, "detail": f"child error: {exc}"}
    finally:
        # The child owns its own cleanup, but a hard timeout SIGKILLs it — so the
        # parent removes the snapshot it named, and sweeps older orphans too.
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
        _sweep_stale_temp(path.parent)


def run(profile: str | None = None) -> dict:
    policy = _policy()
    patterns = policy.get("probe_profiles", DEFAULT_PROBE_PROFILES)
    budget = int(policy.get("probe_budget_s", TOTAL_BUDGET_S))
    started = time.monotonic()
    dbs = []
    for name, path in find_state_dbs(profile, patterns):
        if time.monotonic() - started > budget:
            dbs.append({"profile": name, "path": str(path), "verdict": "unknown",
                        "via": "budget", "size": 0,
                        "detail": f"probe budget {budget}s exhausted"})
            continue
        dbs.append(_classify_isolated(path))
    corrupt = [d["profile"] for d in dbs if d["verdict"] == "corrupted"]
    unknown = [d["profile"] for d in dbs if d["verdict"] == "unknown"]
    status = "down" if corrupt else "ok"
    result = {"ts": _ts(), "iso": _iso(), "status": status, "dbs": dbs,
              "corrupt": corrupt, "unknown": unknown}
    try:
        BOT.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(result, indent=2) + "\n")
    except OSError:
        pass
    return result


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def probe_state_db() -> dict:
    """Lightweight component probe: read the last result (no quick_check)."""
    st = _read_json(STATE, None)
    if not isinstance(st, dict) or not st.get("ts"):
        return {"status": "ok", "detail": "state.db probe not installed / no result yet"}
    age = time.time() - float(st["ts"])
    corrupt = st.get("corrupt") or []
    if corrupt:
        return {"status": "down", "age_s": round(age, 1), "corrupt": corrupt,
                "detail": f"corrupt state.db: {','.join(corrupt)}"}
    if age > PROBE_MAX_AGE_S:
        # Staleness is a timer-liveness problem (probe_timers alerts on it), not
        # a reason to repair the DB. Stay ok so it never triggers repair.
        return {"status": "ok", "age_s": round(age, 1),
                "detail": f"state.db probe stale ({round(age)}s) — check state-db-guard.timer"}
    return {"status": "ok", "age_s": round(age, 1), "unknown": st.get("unknown") or [],
            "detail": "state.db quick_check ok"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Profile state.db integrity probe")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--check-one", default=None, metavar="PATH",
                    help="classify a single DB and print one JSON line (internal)")
    ap.add_argument("--tmp", default=None, metavar="PATH",
                    help="parent-owned snapshot temp path (internal)")
    args = ap.parse_args(argv)
    if args.check_one:
        print(json.dumps(classify_db(Path(args.check_one),
                                     tmp_path=Path(args.tmp) if args.tmp else None),
                         ensure_ascii=False))
        return 0
    result = run(args.profile)
    if args.json:
        print(json.dumps(result, indent=1))
    return 1 if result["status"] == "down" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
