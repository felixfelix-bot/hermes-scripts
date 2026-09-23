#!/usr/bin/env python3
"""Regression tests for decisions_digest.py delivery-state semantics (t_a328b9a0).

The defect these pin (measured 2026-09-23): `last_posted` was stamped BEFORE the
post attempt, so a failed post was recorded as delivered and the whole outage's
queue (20 items, 17 consecutive failing ticks) was silently lost. The fix
advances delivery state ONLY for items that actually posted, leaves a failed
item eligible for the next tick, records health, and prints ALERT/POST-FAIL
markers so the anomaly channel can surface a dead channel.

All network/posting is stubbed: these tests exercise the state machine, never
the real relay. Exit is always 0 (no_agent cron contract).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

DIGEST = Path(__file__).resolve().parents[1] / "decisions_digest.py"


@pytest.fixture()
def dd(tmp_path, monkeypatch):
    """Load the digest module with state/health redirected into tmp_path."""
    monkeypatch.setenv("DECISIONS_STATE_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("DECISIONS_HEALTH_FILE", str(tmp_path / "health.json"))
    monkeypatch.setenv("DECISIONS_RELAY", "wss://relay.test.invalid")
    spec = importlib.util.spec_from_file_location("decisions_digest", DIGEST)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE", tmp_path / "seen.json", raising=True)
    monkeypatch.setattr(mod, "HEALTH", tmp_path / "health.json", raising=True)
    # one synthetic operator-decision item from a stubbed board sweep
    monkeypatch.setattr(mod, "collect_boards", lambda: [{
        "board": "key-security", "task": "t_test", "kind": "blocked",
        "priority": "P1", "title": "operator decision needed",
        "why": "test fixture", "recommend": "A", "reversible": "yes",
    }])
    monkeypatch.setattr(mod, "collect_prs", lambda: [])
    monkeypatch.setattr(mod, "collect_inbound", lambda: [])
    monkeypatch.setattr(mod, "channel", lambda: {"orange_group": "g-test",
                                                "relay": "wss://relay.test.invalid"})
    monkeypatch.setattr(mod, "_bridge_sec", lambda: "nsec1test")
    monkeypatch.setattr(sys, "argv", ["decisions_digest.py"])
    return mod


def _state(dd):
    return json.loads((dd.STATE).read_text())["items"]


def _health(dd):
    return json.loads((dd.HEALTH).read_text())


def test_failed_post_stays_eligible_and_is_not_stamped(dd, monkeypatch, capsys):
    """A failed post must NOT advance delivery state (the original defect)."""
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (False, "relay unreachable"))
    assert dd.main() == 0
    out = capsys.readouterr().out
    assert "posted 0/1" in out
    assert "post failed: relay unreachable" in out
    assert "ALERT decision-channel-degraded" in out

    rec = _state(dd)[dd.key_of(dd.collect_boards()[0])]
    assert rec["sig"] is None, "a failed item must stay undelivered (sig=None)"
    assert "last_posted" not in rec, "last_posted must not be stamped by a failure"
    assert rec["unsent_attempts"] == 1
    assert rec["last_post_error"] == "relay unreachable"


def test_failed_then_successful_tick_delivers_the_backlog(dd, monkeypatch, capsys):
    """The retry path: the SAME item posts on the next tick (nothing lost)."""
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (False, "relay unreachable"))
    dd.main()
    capsys.readouterr()

    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (True, "success"))
    assert dd.main() == 0
    out = capsys.readouterr().out
    assert "to_post=1" in out and "posted 1/1" in out
    rec = _state(dd)[dd.key_of(dd.collect_boards()[0])]
    assert rec["sig"] is not None
    assert "last_posted" in rec
    assert "last_post_error" not in rec
    assert "unsent_attempts" not in rec


def test_delivered_item_is_deduped_on_the_next_tick(dd, monkeypatch, capsys):
    """Dedupe holds: a delivered, unchanged item is not re-posted."""
    posted = []
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (posted.append(text) or (True, "success")))
    dd.main()
    capsys.readouterr()
    assert len(posted) == 1
    assert dd.main() == 0
    out = capsys.readouterr().out
    assert "to_post=0" in out and "posted 0/0" in out
    assert len(posted) == 1, "unchanged delivered item must not post twice"


def test_health_records_failures_and_recovery(dd, monkeypatch, capsys):
    """Health file carries consecutive_failures/last_error, then recovery."""
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (False, "connect failed"))
    dd.main()
    h = _health(dd)
    assert h["consecutive_failures"] == 1
    assert h["last_error"] == "connect failed"
    assert h["posted_last"] == 0 and h["to_post_last"] == 1
    capsys.readouterr()

    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (False, "connect failed"))
    dd.main()
    h = _health(dd)
    assert h["consecutive_failures"] == 2
    capsys.readouterr()

    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (True, "success"))
    assert dd.main() == 0
    out = capsys.readouterr().out
    h = _health(dd)
    assert h["consecutive_failures"] == 0
    assert h["last_success"] > 0
    assert "recovered: decision channel OK again after 2 failing tick(s)" in out


def test_idle_tick_probes_the_channel_and_alerts_when_down(dd, monkeypatch, capsys):
    """An empty queue must still prove liveness — the 17-failure blind spot."""
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (True, "success"))
    dd.main()                      # deliver once so the queue is empty
    capsys.readouterr()

    monkeypatch.setattr(dd, "probe", lambda cfg, relay: ("down", "connection took too long"))
    dd.main()
    out = capsys.readouterr().out
    assert "to_post=0" in out
    assert "ALERT relay-down" in out
    assert "connection took too long" in out
    h = _health(dd)
    assert h["consecutive_failures"] == 1
    assert h["probe"] == "down"


def test_idle_tick_with_healthy_relay_is_silent(dd, monkeypatch, capsys):
    """A healthy empty tick probes, records probe=ok, and raises no alert."""
    monkeypatch.setattr(dd, "post", lambda text, cfg, relay: (True, "success"))
    dd.main()
    capsys.readouterr()
    monkeypatch.setattr(dd, "probe", lambda cfg, relay: ("ok", "authenticating"))
    dd.main()
    out = capsys.readouterr().out
    assert "ALERT" not in out
    assert _health(dd)["probe"] == "ok"
    assert _health(dd)["consecutive_failures"] == 0
