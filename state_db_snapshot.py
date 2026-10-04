#!/usr/bin/env python3
"""state_db_snapshot.py — periodic consistent snapshots of profile state.db files.

Recovery quality at the 2026-09-28 incident depended on how recent a clean copy
existed. This job writes a SQLite backup-API snapshot (WAL-safe) of every profile
`state.db` and keeps:

  * the newest ``KEEP_RECENT`` snapshots (≈24 h at 15-min cadence), and
  * the newest snapshot per calendar day for the last ``DAILY_KEEP`` days.

Snapshots use the same ``state.db.snap-<epoch>`` naming as
``scripts/engine/db_health_check.py`` so either pruner can see them.

Usage:
  state_db_snapshot.py            # snapshot all profiles (staggered)
  state_db_snapshot.py --json
  state_db_snapshot.py --profile manager
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state_db_health import find_state_dbs, selected, snapshot_db  # noqa: E402

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
RESULT = BOT / "state_db_snapshot.json"
POLICY = BOT / "state_db_guard.json"

KEEP_RECENT = int(os.environ.get("STATE_DB_SNAPSHOT_KEEP_RECENT", "96"))
DAILY_KEEP = int(os.environ.get("STATE_DB_SNAPSHOT_DAILY_KEEP", "14"))
STAGGER_S = float(os.environ.get("STATE_DB_SNAPSHOT_STAGGER_S", "2"))
MIN_SNAPSHOT_BYTES = int(os.environ.get("STATE_DB_SNAPSHOT_MIN_BYTES", "1"))

# Rolling profiles are snapshotted every tick; daily profiles once per day. The
# rest (dozens of per-task worktree profiles) are not backed up — they are
# ephemeral and copying them every 15 min would dominate the node's I/O.
DEFAULT_ROLLING = ["manager", "default"]
DEFAULT_DAILY = ["worker-admin", "worker-base", "worker-heavy", "worker-reviewer"]


def select_for_deletion(
    snaps: list[tuple[Path, float]], keep_recent: int, daily_keep: int
) -> list[Path]:
    """Pure retention policy (unit-testable).

    ``snaps`` is a list of (path, mtime). Keeps the newest ``keep_recent`` plus
    the newest per UTC day for the last ``daily_keep`` days; returns the rest.
    """
    ordered = sorted(snaps, key=lambda x: x[1], reverse=True)  # newest first
    keep: set[Path] = {p for p, _ in ordered[: max(0, keep_recent)]}
    per_day: dict[object, Path] = {}
    for p, mt in ordered:
        day = datetime.fromtimestamp(mt, timezone.utc).date()
        per_day.setdefault(day, p)
    for day in sorted(per_day)[-max(0, daily_keep):]:
        keep.add(per_day[day])
    return [p for p, _ in ordered if p not in keep]


def prune(profile_dir: Path, keep_recent: int, daily_keep: int) -> list[str]:
    snaps = []
    for p in profile_dir.glob("state.db.snap-*"):
        if p.name.endswith(("-wal", "-shm")):
            continue
        try:
            snaps.append((p, p.stat().st_mtime))
        except OSError:
            continue
    removed = []
    for p in select_for_deletion(snaps, keep_recent, daily_keep):
        try:
            p.unlink()
            Path(str(p) + "-wal").unlink(missing_ok=True)
            Path(str(p) + "-shm").unlink(missing_ok=True)
            removed.append(p.name)
        except OSError:
            pass
    return removed


def snapshot_one(path: Path) -> dict:
    try:
        if path.stat().st_size < MIN_SNAPSHOT_BYTES:
            return {"path": str(path), "ok": False, "detail": "empty/too small"}
    except OSError as exc:
        return {"path": str(path), "ok": False, "detail": f"stat failed: {exc}"}
    dst = path.parent / f"state.db.snap-{int(time.time())}"
    try:
        snapshot_db(path, dst)
    except Exception as exc:  # noqa: BLE001
        for suffix in ("", "-wal", "-shm"):
            Path(str(dst) + suffix).unlink(missing_ok=True)
        return {"path": str(path), "ok": False, "detail": f"snapshot failed: {exc}"}
    return {"path": str(path), "snap": dst.name, "ok": True,
            "bytes": dst.stat().st_size}


def has_snapshot_today(path: Path) -> bool:
    today = datetime.now(timezone.utc).date()
    for p in path.parent.glob("state.db.snap-*"):
        if p.name.endswith(("-wal", "-shm")):
            continue
        try:
            if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).date() == today:
                return True
        except OSError:
            continue
    return False


def _policy() -> dict:
    try:
        return json.loads(POLICY.read_text())
    except Exception:
        return {}


def _targets(profile: str | None) -> list[tuple[str, Path]]:
    if profile:
        return find_state_dbs(profile)
    policy = _policy()
    rolling = policy.get("rolling_snapshot_profiles", DEFAULT_ROLLING)
    daily = policy.get("daily_snapshot_profiles", DEFAULT_DAILY)
    out = []
    for name, path in find_state_dbs(None, []):
        if selected(rolling, name):
            out.append((name, path))
        elif selected(daily, name) and not has_snapshot_today(path):
            out.append((name, path))
    return out


def run(profile: str | None = None) -> dict:
    results = []
    dbs = _targets(profile)
    for idx, (name, path) in enumerate(dbs):
        if idx and STAGGER_S > 0:
            time.sleep(STAGGER_S)
        res = snapshot_one(path)
        res["profile"] = name
        if res.get("ok"):
            res["removed"] = prune(path.parent, KEEP_RECENT, DAILY_KEEP)
        results.append(res)
    out = {"ts": time.time(),
           "iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "snapshots": results,
           "ok": all(r.get("ok") for r in results) if results else True}
    try:
        BOT.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(out, indent=2) + "\n")
    except OSError:
        pass
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="state.db snapshot + retention")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args(argv)
    result = run(args.profile)
    if args.json:
        print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
