"""Regression tests for the state.db false-corruption repair incident."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import state_db_autorepair as repair  # noqa: E402
import state_db_health as health  # noqa: E402


def test_open_failure_is_unknown_and_cause_is_preserved(monkeypatch, tmp_path):
    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(health.sqlite3, "connect", fail_connect)
    verdict, detail = health.check_conn_ro(tmp_path / "state.db")
    assert verdict == "unknown"
    assert detail == "open failed: database is locked"


def test_busy_quick_check_is_unknown(monkeypatch):
    class BusyConnection:
        def set_progress_handler(self, *args):
            pass

        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

    verdict, detail = health.check_conn(BusyConnection(), 3)
    assert verdict == "unknown"
    assert detail == "quick_check failed: database is locked"


def test_only_completed_row_errors_are_corruption():
    assert health.verdict_from_rows(["*** in database main ***\nPage 4 is never used"]) == (
        "corrupted", "*** in database main ***\nPage 4 is never used")


def test_unknown_health_result_emits_no_repair_action(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    bot = hermes_home / "bot"
    profile = hermes_home / "profiles" / "worker-base"
    profile.mkdir(parents=True)
    (profile / "state.db").touch()
    bot.mkdir()
    (bot / "state_db_health.json").write_text(json.dumps({
        "ts": 1, "corrupt": [], "unknown": ["worker-base"],
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    module = importlib.reload(repair)
    monkeypatch.setattr(module, "find_state_dbs", lambda profile=None: [("worker-base", profile_path)])
    profile_path = profile / "state.db"
    result = module.run(apply=True, profile="worker-base")
    assert result["detail"] == "no snapshot-confirmed corrupt state.db candidates"
    assert result["performed"] == []
    assert not (bot / "fleet_interventions.jsonl").exists()


def test_zero_message_recovery_is_never_applicable():
    best = {"verified": True, "healthy": True, "loss_detected": False, "messages": 0}
    ok, why = repair.may_apply({}, best, incumbent_messages=1000)
    assert not ok
    assert why == "recovery has no messages"


def test_partial_recovery_requires_half_of_incumbent():
    policy = {"allow_lossy": True, "require_lossless": False, "min_recovery_fraction": 0.5}
    best = {"verified": True, "healthy": True, "loss_detected": True, "messages": 499}
    ok, why = repair.may_apply(policy, best, incumbent_messages=1000)
    assert not ok
    assert "requires >= 500" in why
    best["messages"] = 500
    assert repair.may_apply(policy, best, incumbent_messages=1000) == (True, "ok")


def test_single_live_health_sample_requires_snapshot_confirmation(monkeypatch, tmp_path):
    db = tmp_path / "profile" / "state.db"
    db.parent.mkdir()
    db.touch()
    bot = tmp_path / "bot"
    bot.mkdir()
    monkeypatch.setattr(repair, "HEALTH_STATE", bot / "state_db_health.json")
    repair.HEALTH_STATE.write_text(json.dumps({"corrupt": ["profile"]}))
    monkeypatch.setattr(repair, "STATE", bot / "repair-state.json")
    monkeypatch.setattr(repair, "WORK", bot / "work")
    monkeypatch.setattr(repair, "find_state_dbs", lambda profile=None: [("profile", db)])
    monkeypatch.setattr(repair, "find_hermes", lambda: "hermes")
    monkeypatch.setattr(repair, "snapshot_confirmed_corrupt", lambda *_: (False, "snapshot verdict ok"))
    result = repair.run(apply=True, profile="profile")
    assert result["performed"][0]["applied"] is False
    assert result["performed"][0]["detail"] == "snapshot verdict ok"
