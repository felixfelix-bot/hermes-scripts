"""Regression test for the LOCAL offload-hold path (fleet_scheduler.apply_holds).

SCOPE (be precise, do not overclaim):
  * Covered: with hold enabled, the local copy of a card whose winner is a peer
    is blocked locally; a card with no winner is not. This drives the real
    production function.
  * NOT covered: fleet_arbiter per-node remediation intents. As of 2026-10-09 no
    component consumes them (verified: no reader of the intent/lease channel), so
    there is no production behaviour to test. See docs-dispatch-policy.md.

An earlier revision of this file asserted a hand-written literal dict and tested
no production code at all. That was a fake test; it is replaced here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_scheduler as fs  # noqa: E402


def _wire(monkeypatch_free: bool = True):
    """Stub the boundaries apply_holds touches; record kanban calls."""
    calls: list[list[str]] = []
    fs._hold_enabled = lambda: True
    fs._card_status = lambda board, tid: "ready"
    fs._is_review_task = lambda info: False
    fs._hold_ttl = lambda: 1800
    fs._winner_is_live = lambda *a, **k: True

    def _kanban(args):
        calls.append(list(args))
        return (0, "")

    fs._kanban = _kanban
    return calls


def _state(tid: str = "t_1"):
    return {"advertised": {"k1": {"board": "contextvm-services", "task": tid,
                                  "title": "verify pickup checkout"}}}


def test_peer_winner_holds_the_local_copy():
    calls = _wire()
    state = _state()
    n = fs.apply_holds(state, {"k1": "peerX"}, now=1000.0)
    assert n == 1, "expected exactly one local hold"
    assert any("block" in c for c in calls), calls
    assert state["advertised"]["k1"].get("held") == "peerX"


def test_card_without_a_winner_is_not_held():
    calls = _wire()
    state = _state()
    n = fs.apply_holds(state, {}, now=1000.0)
    assert n == 0
    assert calls == [], calls


def test_hold_is_a_no_op_when_disabled():
    calls = _wire()
    fs._hold_enabled = lambda: False
    state = _state()
    n = fs.apply_holds(state, {"k1": "peerX"}, now=1000.0)
    assert n == 0
    assert calls == [], calls
