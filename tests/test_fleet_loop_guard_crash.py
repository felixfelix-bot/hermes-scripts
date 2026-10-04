#!/usr/bin/env python3
"""tests/test_fleet_loop_guard_crash.py — the guard must see crash loops.

Regression for the 2026-09-22 incident: the dispatcher's dead-pid reclaim writes
``outcome='crashed'`` to ``task_runs`` (kanban_db.py), but ``loop_counts`` only
counted ``reclaimed``/``blocked`` — so a card crashing 4-6x/hour reported
"no looping cards" and never got parked. Add a crash family with its own
threshold and recency (mirrors the rate-limit-storm design).

Run:
    /usr/bin/python3 -m pytest tests/test_fleet_loop_guard_crash.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "fleet_loop_guard.py"

_spec = importlib.util.spec_from_file_location("fleet_loop_guard", _SCRIPT)
assert _spec and _spec.loader, f"cannot build spec for {_SCRIPT}"
g = importlib.util.module_from_spec(_spec)
sys.modules["fleet_loop_guard"] = g
_spec.loader.exec_module(g)

NOW = 1_800_000_000.0


def _crash(n, ts=NOW):
    return [(ts + i, "crashed", "crashed") for i in range(n)]


class TestCrashFamily:
    def test_crash_outcomes_are_declared(self):
        for o in ("crashed", "stale", "timed_out", "spawn_failed"):
            assert o in g.CRASH_OUTCOMES

    def test_loop_counts_reports_crashes(self):
        bad, rl, crash = g.loop_counts(_crash(3))
        assert bad == 0 and rl == 0 and crash == 3

    def test_a_few_crashes_are_not_yet_a_loop(self):
        runs = _crash(2)
        assert not g.is_looping(runs, threshold=2, rl_threshold=6,
                                crash_threshold=4, now=NOW + 2,
                                rl_recency_s=3600, crash_recency_s=3600)

    def test_repeated_crashes_are_a_loop(self):
        runs = _crash(4)
        assert g.is_looping(runs, threshold=2, rl_threshold=6,
                            crash_threshold=4, now=NOW + 4,
                            rl_recency_s=3600, crash_recency_s=3600)

    def test_stale_crash_streak_is_not_a_loop(self):
        """A recovered lane stops crashing, so an old streak must not park it."""
        runs = _crash(4, ts=NOW - 10 * 3600)   # newest is 10h old
        assert not g.is_looping(runs, threshold=2, rl_threshold=6,
                                crash_threshold=4, now=NOW,
                                rl_recency_s=3600, crash_recency_s=45 * 60)

    def test_reclaimed_and_blocked_still_count_as_bad(self):
        runs = [(NOW, "reclaimed", "reclaimed"), (NOW, "blocked", "blocked")]
        bad, rl, crash = g.loop_counts(runs)
        assert bad == 2 and crash == 0

    def test_crash_and_rl_are_independent(self):
        runs = _crash(4) + [(NOW, "rate_limited", "rate_limited")]
        bad, rl, crash = g.loop_counts(runs)
        assert bad == 0 and rl == 1 and crash == 4

    def test_crash_block_after_config_default(self):
        assert isinstance(g.config_crash_threshold(), int)
        assert g.config_crash_threshold() > 0
