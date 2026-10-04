#!/usr/bin/env python3
"""tests/test_task_lifecycle_governor_revive.py — no block<->revive ping-pong.

Regression for the 2026-09-22 "CARD LOOP ... burning worker slots" incident:
the governor classified a dead-worker block (``last_failure_error`` contains
"not alive") as a crash-loop and re-readied it every cooldown forever, undoing
the loop-guard / dispatcher-breaker block 10 minutes later. A card died 4-6x/h.

The governor must NOT revive:
  * a card parked by a guard/breaker (reason carries a guard marker), or
  * a crash-loop that has already been revived ``TASK_REVIVE_MAX`` times.

Run:
    /usr/bin/python3 -m pytest tests/test_task_lifecycle_governor_revive.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "task-lifecycle-governor.py"

_spec = importlib.util.spec_from_file_location("task_lifecycle_governor", _SCRIPT)
assert _spec and _spec.loader, f"cannot build spec for {_SCRIPT}"
gov = importlib.util.module_from_spec(_spec)
sys.modules["task_lifecycle_governor"] = gov
_spec.loader.exec_module(gov)


def _guard_reason(text):
    """Mirror _guard_block_reason's marker scan without a DB."""
    hay = str(text).lower()
    return next((m for m in gov.GUARD_MARKERS if m in hay), "")


class TestGuardMarkers:
    def test_reclaim_loop_is_guard_parked(self):
        assert _guard_reason("reclaim loop: worker died 3x (dispatcher guard)")

    def test_reclaim_backoff_is_guard_parked(self):
        assert _guard_reason("reclaim backoff until 1790075351 (dead worker pid, strike 1)")

    def test_fleet_loop_guard_is_guard_parked(self):
        assert _guard_reason("auto: looped 4x ... blocked by fleet_loop_guard")

    def test_circuit_breaker_is_guard_parked(self):
        assert _guard_reason("circuit breaker: 3 consecutive crashed runs")

    def test_plain_crash_is_not_guard_parked(self):
        assert _guard_reason("pid 485412 not alive") == ""

    def test_empty_is_not_guard_parked(self):
        assert _guard_reason("") == ""


class TestReviveDecision:
    def test_plain_crash_within_budget_revives(self):
        assert gov._revive_decision("", 0) == "revive"
        assert gov._revive_decision("", gov.MAX_REVIVES - 1) == "revive"

    def test_guard_parked_never_revives(self):
        assert gov._revive_decision("reclaim loop", 0) == "guard-parked"
        assert gov._revive_decision("fleet_loop_guard", 0) == "guard-parked"

    def test_revive_cap_stops_the_ping_pong(self):
        assert gov._revive_decision("", gov.MAX_REVIVES) == "capped"
        assert gov._revive_decision("", gov.MAX_REVIVES + 5) == "capped"

    def test_cap_is_configurable(self):
        assert gov._revive_decision("", 2, max_revives=2) == "capped"
        assert gov._revive_decision("", 1, max_revives=2) == "revive"

    def test_guard_wins_over_remaining_budget(self):
        assert gov._revive_decision("dispatcher guard", 0, max_revives=99) == "guard-parked"
