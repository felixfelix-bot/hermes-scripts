#!/usr/bin/env python3
"""Retention policy tests for state_db_snapshot.py.

Regression: count-only retention (KEEP_RECENT=8) multiplied by a 650 MB
state.db kept ~5.2 GB of snapshots per profile and pushed / to 96% full, which
surfaced as "disk full: session storage could not be written" and killed
background-delegation final reports (2026-10-07).

So retention must also be capped BY BYTES, and the job must not create a new
snapshot when the filesystem is already low.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state_db_snapshot import free_gb, select_for_deletion  # noqa: E402

MB = 1024 * 1024


def _snap(p: Path, age_s: float) -> tuple:
    p.write_bytes(b"x")
    return (p, time.time() - age_s)


def test_byte_cap_prunes_oldest_beyond_the_cap(tmp_path):
    snaps = [_snap(tmp_path / f"state.db.snap-{i}", i * 60) for i in range(9)]
    sizes = {p: 600 * MB for p, _ in snaps}
    # 9 x 600 MB = 5.4 GB with a 1.5 GB cap -> keep the newest that fit (2), plus the newest.
    doomed = select_for_deletion(snaps, keep_recent=8, daily_keep=3,
                                 max_bytes=1536 * MB, sizes=sizes)
    newest = snaps[0][0]
    assert newest not in doomed
    assert len(snaps) - len(doomed) == 2
    # oldest gone first
    assert snaps[-1][0] in doomed
    assert snaps[1][0] not in doomed


def test_byte_cap_never_deletes_the_newest_snapshot(tmp_path):
    snaps = [_snap(tmp_path / f"state.db.snap-{i}", i * 60) for i in range(3)]
    sizes = {p: 5 * 1024 * MB for p, _ in snaps}
    doomed = select_for_deletion(snaps, keep_recent=8, daily_keep=3,
                                 max_bytes=1 * MB, sizes=sizes)
    assert snaps[0][0] not in doomed


def test_count_policy_still_applies_when_no_byte_cap(tmp_path):
    snaps = [_snap(tmp_path / f"state.db.snap-{i}", i * 60) for i in range(6)]
    doomed = select_for_deletion(snaps, keep_recent=2, daily_keep=1)
    assert len(doomed) == 4
    assert {snaps[0][0], snaps[1][0]}.isdisjoint(doomed)


def test_free_gb_reports_something_real(tmp_path):
    assert free_gb(tmp_path) > 0


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python3", "-m", "pytest", "-q", __file__]))
