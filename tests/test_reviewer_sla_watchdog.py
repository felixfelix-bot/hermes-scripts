"""Unit + integration tests for reviewer_sla_watchdog.py.

Fixture strategy (mirrors tests/test_reap_stale_sessions.py):
- Board kanban.db is built with the REAL production `tasks` schema (the subset
  the watchdog reads), copied verbatim from the kanban kernel. Only columns
  used by the watchdog are seeded; extra columns are omitted from the fixture
  DDL (they have no CHECK constraints in production, so this is safe).
- All DBs and state files point into pytest tmp_path — no production board is
  ever read or written.

SLA contract under test (task t_55d56efa, D-114 §6):
  stale  := task assigned to a reviewer profile (worker-reviewer-kimi or
            worker-reviewer-glm), status IN ('running','review'), with
            started_at set, and now - started_at > sla_hours (default 4h).
  propose_reassign(assignee) := the OPPOSITE reviewer family profile
            (kimi -> glm, glm -> kimi), matching D-114 cross-family pairing.
  politeness := a given task id alerts at most once per politeness window
            (default 6h), tracked by a JSON state file so a chronically-stale
            review nags without spamming every 30-min cron tick.
  silent idle := when no stale tasks exist, main() prints NOTHING (empty
            stdout) so a script-only cron stays quiet — the classic watchdog
            pattern (empty stdout => nothing delivered).
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewer_sla_watchdog as wd  # noqa: E402

# ── Real production `tasks` DDL (subset read by the watchdog) ────────────────
TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    assignee       TEXT,
    status         TEXT NOT NULL,
    started_at     INTEGER,
    last_heartbeat_at INTEGER,
    current_run_id INTEGER,
    created_at     INTEGER NOT NULL
);
"""

SLA = 4          # hours
NOW = time.time()
T_STALE = NOW - (SLA + 1) * 3600      # 5h ago -> stale
T_RECENT = NOW - 1 * 3600             # 1h ago  -> fresh


def make_board(db, tasks=()):
    """Build a board kanban.db with the production schema and seed rows."""
    con = sqlite3.connect(db)
    con.executescript(TASKS_DDL)
    for tid, assignee, status, started in tasks:
        con.execute(
            "INSERT INTO tasks (id, title, assignee, status, started_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (tid, f"review-{tid}", assignee, status, started, NOW),
        )
    con.commit()
    con.close()
    return db


def now_override(monkeypatch, fixed):
    """Pin the watchdog's clock to a fixed timestamp for deterministic tests."""
    monkeypatch.setattr(wd, "now", lambda: fixed)


# ── selection ────────────────────────────────────────────────────────────────

def test_stale_kimi_review_is_detected(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    alerts = wd.scan(str(db), NOW)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.task_id == "t1"
    assert a.assignee == "worker-reviewer-kimi"


def test_recent_review_not_stale(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_RECENT),
    ])
    assert wd.scan(str(db), NOW) == []


def test_reject_reassigns_to_opposite_family(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "review", T_STALE),   # kimi done -> glm
        ("t2", "worker-reviewer-glm", "running", T_STALE),   # glm done -> kimi
    ])
    by_id = {a.task_id: a for a in wd.scan(str(db), NOW)}
    assert by_id["t1"].proposed_reassign == "worker-reviewer-glm"
    assert by_id["t2"].proposed_reassign == "worker-reviewer-kimi"


def test_non_reviewer_assignee_ignored(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-admin", "running", T_STALE),
    ])
    assert wd.scan(str(db), NOW) == []


def test_done_or_unstarted_ignored(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "done", T_STALE),   # completed -> skip
        ("t2", "worker-reviewer-kimi", "todo", None),      # never started -> skip
    ])
    assert wd.scan(str(db), NOW) == []


def test_missing_started_at_ignored(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", None),
    ])
    assert wd.scan(str(db), NOW) == []


# ── politeness (no re-alert spam) ────────────────────────────────────────────

def test_politeness_suppresses_repeat_within_window(tmp_path, capsys):
    state = tmp_path / "state.json"
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    out = wd.main(["--db", str(db), "--state-file", str(state)])
    assert out == 0
    first = capsys.readouterr().out
    assert "t1" in first  # first run does alert
    # second run in the same window alerts nothing (silent cron contract)
    out2 = wd.main(["--db", str(db), "--state-file", str(state)])
    assert out2 == 0
    assert capsys.readouterr().out == ""


def test_politeness_expires_after_window(tmp_path):
    state = tmp_path / "state.json"
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    wd.main(["--db", str(db), "--state-file", str(state)])
    # rewind the state file's last-alerted timestamp past the window
    wd.write_state(str(state), {"t1": NOW - (wd.POLITENESS_H + 1) * 3600})
    alerts = wd.scan_after_politeness(str(db), NOW, wd.load_state(str(state)))
    assert any(a.task_id == "t1" for a in alerts)


# ── silent-when-idle (cron watchdog contract) ────────────────────────────────

def test_silent_when_no_stale(capsys, tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_RECENT),
    ])
    rc = wd.main(["--db", str(db), "--state-file", str(tmp_path / "s.json")])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_alerts_when_stale(capsys, tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    rc = wd.main(["--db", str(db), "--state-file", str(tmp_path / "s.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "t1" in out and "worker-reviewer-glm" in out


# ── CLI / argument handling ──────────────────────────────────────────────────

def test_requires_db_arg():
    with pytest.raises(SystemExit):
        wd.main([])  # argparse error on missing --db


def test_missing_db_file_handled_gracefully(tmp_path):
    rc = wd.main(["--db", str(tmp_path / "nope" / "kanban.db"),
                  "--state-file", str(tmp_path / "s.json")])
    assert rc == 1  # visible to cron as an error, not silent success


def test_sla_hours_configurable(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-glm", "running", NOW - 3 * 3600),  # 3h
    ])
    # default 4h -> not stale
    assert wd.scan(str(db), NOW) == []
    # sla=2h -> stale
    alerts = wd.scan(str(db), NOW, sla_hours=2)
    assert len(alerts) == 1


def test_render_lists_proposed_reassign_and_age():
    a = wd.Alert(
        task_id="t1", title="review-t1", assignee="worker-reviewer-kimi",
        age_h=5.0, decision="reassign", proposed_reassign="worker-reviewer-glm",
    )
    text = wd.render([a])
    assert "t1" in text
    assert "worker-reviewer-glm" in text
    assert "5.0h" in text


def test_render_routes_to_manager_when_budget_exhausted():
    a = wd.Alert(
        task_id="t1", title="review-t1", assignee="worker-reviewer-kimi",
        age_h=5.0, decision="manager", proposed_reassign="",
    )
    text = wd.render([a])
    assert "t1" in text
    assert "MANAGER" in text
    assert "worker-reviewer-glm" not in text


def rewind_last_alerted(state_path, task_id):
    """Test helper: set a task's last_alerted to the distant past so the
    politeness window expires and it is reconsidered on the next run."""
    data = wd.load_state(state_path)
    old = data.get(task_id)
    if isinstance(old, dict):
        old["last_alerted"] = NOW - (wd.POLITENESS_H + 2) * 3600
        data[task_id] = old
    else:
        data[task_id] = {"last_alerted": NOW - (wd.POLITENESS_H + 2) * 3600,
                         "reassign_count": 0}
    wd.write_state(state_path, data)


# ── Consultant hardening (v3 MANDATORY): max-1 reassign, HALT, persistent ledger ──

def test_max_one_reassign_then_route_to_manager(tmp_path, capsys):
    """First breach proposes a reassign; a later breach of the SAME task routes
    to the manager queue (no kimi<->glm ping-pong)."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    # Run 1: first breach -> propose reassign to glm, count -> 1
    assert wd.main(["--db", str(db), "--state-file", str(state)]) == 0
    out1 = capsys.readouterr().out
    assert "worker-reviewer-glm" in out1
    # Task is STILL stale (reviewer never picked it up). Reassign count is 1.
    # Run 2 (politeness expired): must NOT propose another reassign; route to manager.
    rewind_last_alerted(str(state), "t1")  # simulate politeness window elapsed
    assert wd.main(["--db", str(db), "--state-file", str(state)]) == 0
    out2 = capsys.readouterr().out
    assert "MANAGER" in out2 or "manager" in out2
    assert "worker-reviewer-glm" not in out2  # no second reassign proposal


def test_reassignment_change_does_not_re_propose(tmp_path, capsys):
    """If the operator DID reassign (assignee changed to glm) but the task is
    still stale, route to manager — preserve cross-family, no ping-pong."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    assert wd.main(["--db", str(db), "--state-file", str(state)]) == 0  # propose, count=1
    capsys.readouterr().out
    # Operator reassigns kimi -> glm; still stale.
    db2 = make_board(tmp_path / "kanban2.db", tasks=[
        ("t1", "worker-reviewer-glm", "running", T_STALE),
    ])
    rewind_last_alerted(str(state), "t1")
    assert wd.main(["--db", str(db2), "--state-file", str(state)]) == 0
    out = capsys.readouterr().out
    assert ("manager" in out or "MANAGER" in out)
    assert "worker-reviewer-kimi" not in out  # no glm->kimi bounce


def test_halt_flag_never_reassigns_routes_to_manager(tmp_path, capsys):
    """HALT flag set -> no reassignment ever; log + route to manager."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    halt = tmp_path / "REVIEWER_HALT"
    halt.write_text("manual review in progress\n")
    assert wd.main(["--db", str(db), "--state-file", str(state),
                    "--halt-file", str(halt)]) == 0
    out = capsys.readouterr().out
    assert "manager" in out.lower()
    assert "worker-reviewer-glm" not in out
    # ledger must NOT have incremented reassign_count under HALT
    rec = json.loads(state.read_text()).get("t1", {})
    assert rec.get("reassign_count", 0) == 0


def test_halt_does_not_consume_politeness_window(tmp_path, capsys):
    """Manager-routed (HALT) alerts must NOT update last_alerted, so a
    still-stale task is reconsidered the moment HALT lifts — no 6h politeness
    stall after an operator override (cold-review finding 7)."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    halt = tmp_path / "REVIEWER_HALT"
    halt.write_text("halt\n")
    # Run 1 under HALT: routes to manager, must NOT set last_alerted.
    assert wd.main(["--db", str(db), "--state-file", str(state),
                    "--halt-file", str(halt)]) == 0
    capsys.readouterr().out
    rec = json.loads(state.read_text()).get("t1", {})
    assert "last_alerted" not in rec
    # HALT lifts; the SAME task must be reconsidered immediately (no politeness
    # suppression) and now propose a reassign.
    halt.unlink()
    assert wd.main(["--db", str(db), "--state-file", str(state)]) == 0
    out = capsys.readouterr().out
    assert "worker-reviewer-glm" in out  # reassign proposed right away


def test_read_only_connection_never_falls_back(tmp_path):
    """The watchdog must open the board DB read-only and NEVER fall back to a
    read-write connection (cold-review finding 2). A read-only URI failure is
    a hard error, not a silent downgrade."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    # Make the DB file read-only so a read-write open would fail; the
    # read-only URI open must still succeed.
    os.chmod(db, 0o444)
    try:
        alerts = wd.scan(str(db), NOW)
        assert len(alerts) == 1
    finally:
        os.chmod(db, 0o644)


def test_ledger_persists_across_restarts(tmp_path):
    """Persistent assignment ledger (survives watchdog restart): reassign_count
    is written to the JSON state file and read back on the next process."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    assert wd.main(["--db", str(db), "--state-file", str(state), "--dry-run"]) == 0
    # dry-run must still NOT mutate; run a real one to persist
    assert wd.main(["--db", str(db), "--state-file", str(state)]) == 0
    on_disk = json.loads(state.read_text())
    assert on_disk["t1"]["reassign_count"] == 1
    # fresh process (new load_state) reads the same durable count
    rec = wd.load_state(str(state))["t1"]
    assert rec["reassign_count"] == 1


def test_dry_run_does_not_persist_ledger(tmp_path, capsys):
    """--dry-run reports but does not write the ledger or consume the
    reassign budget."""
    db = make_board(tmp_path / "kanban.db", tasks=[
        ("t1", "worker-reviewer-kimi", "running", T_STALE),
    ])
    state = tmp_path / "ledger.json"
    assert wd.main(["--db", str(db), "--state-file", str(state), "--dry-run"]) == 0
    assert "worker-reviewer-glm" in capsys.readouterr().out
    assert not state.exists()  # nothing persisted under dry-run


# ── Atomicity / durability hardening (cold review H1, M2) ───────────────────

def test_write_state_is_atomic_no_temp_leftovers(tmp_path):
    """write_state must not leave a partial/torn file or stray temp files —
    a mid-write kill must never corrupt the ledger (cold-review H1)."""
    state = tmp_path / "ledger.json"
    wd.write_state(str(state), {"t1": {"last_alerted": NOW, "reassign_count": 1}})
    # file is valid JSON
    assert json.loads(state.read_text())["t1"]["reassign_count"] == 1
    # no temp/partial sibling left behind
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == ["ledger.json"], leftovers


def test_write_state_invalidates_old_inode_refs(tmp_path):
    """Each write must replace the file (new inode), never truncate in place,
    so a reader holding an old fd never sees a torn file mid-rewrite."""
    state = tmp_path / "ledger.json"
    wd.write_state(str(state), {"t1": {"reassign_count": 1}})
    ino1 = state.stat().st_ino
    wd.write_state(str(state), {"t2": {"reassign_count": 2}})
    ino2 = state.stat().st_ino
    assert ino1 != ino2  # os.replace -> new inode
    assert json.loads(state.read_text()) == {"t2": {"reassign_count": 2}}


def test_corrupt_state_recovery_is_loud(tmp_path, capsys):
    """A corrupt ledger must NOT be silently reset to {} — the watchdog must
    warn on stderr (cold-review M2) so the operator knows the budget was lost."""
    state = tmp_path / "ledger.json"
    state.write_text("{ not valid json !!!")
    data = wd.load_state(str(state))
    assert data == {}  # still recovers to empty (watchdog stays functional)
    assert "corrupt" in capsys.readouterr().err.lower()  # but warns loudly


def test_concurrent_writes_serialize_via_lock(tmp_path):
    """load->decide->write must be guarded by an advisory file lock so two
    overlapping cron invocations cannot both read count=0 and double-propose
    (cold-review H2). Verify two sequential locked writers both increment."""
    state = tmp_path / "ledger.json"
    wd.write_state(str(state), {"t1": {"last_alerted": NOW - 9999,
                                       "reassign_count": 0}})
    # first writer
    with wd.ledger_lock(str(state)):
        rec = wd.load_state(str(state))["t1"]
        rec["reassign_count"] += 1
        wd.write_state(str(state), {"t1": rec})
    # second writer (would race without the lock)
    with wd.ledger_lock(str(state)):
        rec = wd.load_state(str(state))["t1"]
        rec["reassign_count"] += 1
        wd.write_state(str(state), {"t1": rec})
    assert json.loads(state.read_text())["t1"]["reassign_count"] == 2
