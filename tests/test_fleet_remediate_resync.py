"""Tests for fleet_remediate.py resync-kanban feedback-loop guards.

Covers the two guards that break the health->remediation loop:
  (i) rate limiter — min 15 min between runs AND max 8 runs / rolling 24h.
  (ii) content freshness — a resync is only needed when a board DB is newer
      (mtime) than the last successful sync; never on repo weight.

All decision helpers are pure (explicit ``now`` / explicit paths), so the tests
are deterministic and never touch the real ~/.hermes/bot state files.
"""
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import fleet_remediate as fr  # noqa: E402

NOW = 1_800_000_000.0


# ── (i) rate limiter ──────────────────────────────────────────────────────────

def test_rate_limiter_allows_first_run():
    blocked, why = fr._resync_rate_limited([], NOW)
    assert blocked is False
    assert why == ""


def test_rate_limiter_blocks_second_within_15min():
    runs = [NOW - 60]  # 60 s ago, inside the 900 s min interval
    blocked, why = fr._resync_rate_limited(runs, NOW)
    assert blocked is True
    assert why == "min-interval"


def test_rate_limiter_allows_after_15min():
    runs = [NOW - 900]  # exactly at the boundary: not < 900 -> allowed
    blocked, why = fr._resync_rate_limited(runs, NOW)
    assert blocked is False


def test_rate_limiter_caps_at_8_per_24h():
    # 8 runs spread >15 min apart, all within 24h -> the 9th is blocked.
    runs = [NOW - (i * 1000) for i in range(8)]  # 0, -1000, ... -7000 s
    blocked, why = fr._resync_rate_limited(runs, NOW)
    assert blocked is True
    assert why == "24h-cap"


def test_rate_limiter_seven_runs_allowed():
    runs = [NOW - (i * 1000) for i in range(7)]
    blocked, _ = fr._resync_rate_limited(runs, NOW)
    assert blocked is False


def test_rate_limiter_old_runs_outside_24h_window_ignored():
    # 8 runs, but the oldest is >24h old -> only 7 count, so allowed.
    runs = [NOW - (i * 1000) for i in range(8)]
    runs[-1] = NOW - 90000  # > 24h ago
    blocked, _ = fr._resync_rate_limited(runs, NOW)
    assert blocked is False


def test_rate_limiter_min_interval_checked_before_cap():
    # A run 1 s ago blocks on min-interval even if the 24h count is low.
    runs = [NOW - 1]
    blocked, why = fr._resync_rate_limited(runs, NOW)
    assert blocked is True
    assert why == "min-interval"


def test_rate_limiter_state_persistence(tmp_path):
    # _load_resync_runs / _record_resync_run round-trip through a JSON file.
    path = tmp_path / "ratelimit.json"
    fr._record_resync_run(path, [], NOW)
    fr._record_resync_run(path, fr._load_resync_runs(path), NOW + 1000)
    runs = fr._load_resync_runs(path)
    assert runs == [NOW, NOW + 1000]
    # and a window prune drops runs older than 24h
    fr._record_resync_run(path, [NOW - 90000], NOW)
    assert fr._load_resync_runs(path) == [NOW]


# ── (ii) content freshness ───────────────────────────────────────────────────

def _make_board_db(boards_root, name, mtime):
    d = boards_root / name
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    db.write_text("")
    os.utime(db, (mtime, mtime))
    return db


def test_content_stale_when_db_newer_than_last_sync(tmp_path):
    boards = tmp_path / "boards"
    _make_board_db(boards, "b1", NOW - 100)  # synced 100 s ago
    assert fr._content_is_stale(boards, last_sync_ts=NOW - 200) is True


def test_content_fresh_when_db_older_than_last_sync(tmp_path):
    boards = tmp_path / "boards"
    _make_board_db(boards, "b1", NOW - 500)
    assert fr._content_is_stale(boards, last_sync_ts=NOW - 100) is False


def test_content_fresh_at_exact_boundary(tmp_path):
    boards = tmp_path / "boards"
    _make_board_db(boards, "b1", NOW - 100)
    # mtime == last_sync -> not stale (strict >)
    assert fr._content_is_stale(boards, last_sync_ts=NOW - 100) is False


def test_content_not_stale_with_no_boards(tmp_path):
    boards = tmp_path / "boards"
    boards.mkdir()
    assert fr._content_is_stale(boards, last_sync_ts=0.0) is False


def test_content_newest_db_wins(tmp_path):
    boards = tmp_path / "boards"
    _make_board_db(boards, "old", NOW - 1000)
    _make_board_db(boards, "new", NOW - 50)
    # newest (NOW-50) > last sync (NOW-100) -> stale
    assert fr._content_is_stale(boards, last_sync_ts=NOW - 100) is True


def test_last_sync_ts_missing_state(tmp_path):
    # No state file -> last sync 0 -> any board is stale.
    assert fr._last_sync_ts(tmp_path / "absent.json") == 0.0
