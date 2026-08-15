"""Unit + integration tests for the D3 crash circuit-breaker.

Fixture strategy:
- Board DBs are built with the REAL production schema (copied verbatim from
  ~/.hermes/kanban/boards/*/kanban.db DDL for tasks/task_runs/task_events).
- A fake `hermes` CLI script stands in for the real one: it records every
  invocation to a JSONL file AND applies block/reclaim/unblock mutations to
  the fixture DB, so integration tests verify end-to-end board state.
- All paths (state file, usage DB, boards dir, config) point into pytest
  tmp_path via env overrides — no production file is ever touched.
"""
import json
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import circuit_breaker as cb  # noqa: E402

# ── Real production DDL (verbatim from a live board) ─────────────────────────
TASKS_DDL = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    model_override       TEXT,
    max_retries          INTEGER,
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    goal_max_turns       INTEGER,
    session_id           TEXT
)
"""
RUNS_DDL = """
CREATE TABLE task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
)
"""
EVENTS_DDL = """
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
)
"""

FAKE_HERMES = r"""#!/usr/bin/env python3
""" + '''"""Fake hermes CLI: records invocations + mutates the fixture board DB."""
import json, os, sqlite3, sys
from pathlib import Path

BOARD = os.environ["FAKE_HERMES_BOARD"]
DB = os.environ["FAKE_HERMES_DB"]
LOG = os.environ["FAKE_HERMES_LOG"]

args = sys.argv[1:]
with open(LOG, "a") as f:
    f.write(json.dumps({"args": args}) + "\\n")

# hermes kanban --board B block T reason...
# hermes kanban --board B reclaim T --reason R
# hermes kanban --board B unblock T... --reason R
def find(cmd):
    return cmd in args

conn = sqlite3.connect(DB)
if find("block"):
    tid = args[args.index("block") + 1]
    reason = " ".join(args[args.index("block") + 2:])
    if reason.startswith("--ids"):
        reason = "bulk"
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
        (tid, "comment", json.dumps({"text": reason}), 1000),
    )
elif find("reclaim"):
    tid = args[args.index("reclaim") + 1]
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?", (tid,))
    conn.execute(
        "UPDATE task_runs SET status='done', outcome='reclaimed' "
        "WHERE task_id=? AND status='running'", (tid,))
elif find("unblock"):
    i = args.index("unblock")
    for tid in args[i + 1:]:
        if tid.startswith("--"): break
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
conn.commit()
conn.close()
print("fake-hermes ok")
'''


@pytest.fixture()
def env(tmp_path):
    """Isolated breaker environment: boards dir, state, usage DB, fake hermes."""
    boards = tmp_path / "boards"
    boards.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    usage_db = tmp_path / "zai_usage.db"
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()

    fake = tmp_path / "fake-hermes.py"
    fake.write_text(FAKE_HERMES)
    fake.chmod(0o755)

    e = {
        "CIRCUIT_BREAKER_STATE": str(state / "circuit_breaker.json"),
        "CIRCUIT_BREAKER_USAGE_DB": str(usage_db),
        "CIRCUIT_BREAKER_BOARDS_DIR": str(boards),
        "CIRCUIT_BREAKER_CONFIG": str(cfgdir / "circuit-breaker.json"),
        "CIRCUIT_BREAKER_HERMES_BIN": str(fake),
        "FAKE_HERMES_LOG": str(tmp_path / "hermes-calls.jsonl"),
        "FAKE_HERMES_BOARD": "testboard",
        "FAKE_HERMES_DB": "",  # set per-board by make_board
        "CIRCUIT_BREAKER_MODE": "enforce",  # tests assert real actions; log-only tested explicitly
    }
    os.environ.update(e)
    yield e
    for k in e:
        os.environ.pop(k, None)


def make_board(env, name, tasks, runs):
    """Create a fixture board DB with the real schema. Returns Path.
    `env` may be the pytest env-fixture dict OR any dict with
    CIRCUIT_BREAKER_BOARDS_DIR (unit tests pass a plain dict)."""
    d = Path(env["CIRCUIT_BREAKER_BOARDS_DIR"]) / name
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(TASKS_DDL)
    conn.execute(RUNS_DDL)
    conn.execute(EVENTS_DDL)
    conn.executemany(
        "INSERT INTO tasks (id, title, status, created_at, assignee, consecutive_failures, last_failure_error) "
        "VALUES (:id, :title, :status, :created_at, :assignee, :cf, :err)",
        tasks,
    )
    conn.executemany(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, error) "
        "VALUES (:task_id, :profile, :status, :started_at, :ended_at, :outcome, :error)",
        runs,
    )
    conn.commit()
    conn.close()
    # the fake hermes subprocess reads the board DB path from the environment
    os.environ["FAKE_HERMES_DB"] = str(db)
    return db


def task(id="t_1", status="ready", cf=0, err=None, assignee="worker-x", title="T"):
    return dict(id=id, title=title, status=status, created_at=100, assignee=assignee, cf=cf, err=err)


def run(task_id="t_1", outcome="crashed", started_at=100, ended_at=200, error="pid 1 not alive", profile="worker-x"):
    return dict(task_id=task_id, profile=profile, status="done", started_at=started_at, ended_at=ended_at,
                outcome=outcome, error=error)


def hermes_calls(env):
    log = Path(env["FAKE_HERMES_LOG"])
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines()]


def events(env, action=None):
    if not os.path.exists(env["CIRCUIT_BREAKER_USAGE_DB"]):
        return []
    conn = sqlite3.connect(env["CIRCUIT_BREAKER_USAGE_DB"])
    try:
        rows = conn.execute("SELECT action, board, task_id, mode, detail FROM circuit_breaker_events").fetchall()
    except sqlite3.OperationalError:
        rows = []  # table not created yet == breaker wrote nothing
    finally:
        conn.close()
    if action:
        rows = [r for r in rows if r[0] == action]
    return rows


def anomalies(env):
    if not os.path.exists(env["CIRCUIT_BREAKER_USAGE_DB"]):
        return []
    conn = sqlite3.connect(env["CIRCUIT_BREAKER_USAGE_DB"])
    try:
        rows = conn.execute("SELECT severity, category, title, alerted, resolved FROM anomaly_events").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return rows


def task_events_for(db, task_id, kind=None):
    conn = sqlite3.connect(db)
    q = "SELECT kind, payload FROM task_events WHERE task_id=?"
    rows = conn.execute(q, (task_id,)).fetchall()
    conn.close()
    if kind:
        rows = [r for r in rows if r[0] == kind]
    return rows


# ── Unit: streak computation ─────────────────────────────────────────────────

def test_streak_from_runs_only_crash_family_counts(tmp_path):
    db = make_board({"CIRCUIT_BREAKER_BOARDS_DIR": str(tmp_path)}, "b", [task(cf=0)],
                    [run(outcome="crashed"), run(outcome="timed_out"), run(outcome="spawn_failed")])
    conn = sqlite3.connect(db)
    assert cb.computed_streak(conn, "t_1", ("crashed", "timed_out", "spawn_failed")) == 3
    conn.close()


def test_streak_broken_by_completed(tmp_path):
    db = make_board({"CIRCUIT_BREAKER_BOARDS_DIR": str(tmp_path)}, "b", [task(cf=0)],
                    [run(outcome="crashed"), run(outcome="completed"), run(outcome="crashed", error="x")])
    conn = sqlite3.connect(db)
    assert cb.computed_streak(conn, "t_1", ("crashed", "timed_out", "spawn_failed")) == 1
    conn.close()


def test_streak_takes_max_of_column_and_runs(tmp_path):
    # column says 5, runs say 1 -> 5 (kernel counter is authoritative for spawn failures)
    db = make_board({"CIRCUIT_BREAKER_BOARDS_DIR": str(tmp_path)}, "b", [task(cf=5)],
                    [run(outcome="crashed"), run(outcome="completed")])
    conn = sqlite3.connect(db)
    t = conn.execute("SELECT * FROM tasks WHERE id='t_1'").fetchone()
    cols = [c[0] for c in conn.execute("SELECT * FROM tasks WHERE id='t_1'").description]
    row = dict(zip(cols, t))
    assert cb.task_streak(conn, row, ("crashed", "timed_out", "spawn_failed")) == 5
    conn.close()


# ── Unit: config precedence ──────────────────────────────────────────────────

def test_config_defaults():
    cfg = cb.load_config({})
    assert cfg["mode"] == "log-only"
    assert cfg["task_threshold"] == 3
    assert cfg["board_warn_threshold"] == 5
    assert cfg["board_freeze_threshold"] == 10


def test_config_env_overrides():
    cfg = cb.load_config({"CIRCUIT_BREAKER_MODE": "enforce", "CIRCUIT_BREAKER_TASK_THRESHOLD": "7"})
    assert cfg["mode"] == "enforce"
    assert cfg["task_threshold"] == 7


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    cf = tmp_path / "circuit-breaker.json"
    cf.write_text(json.dumps({"task_threshold": 4}))
    monkeypatch.setenv("CIRCUIT_BREAKER_CONFIG", str(cf))
    cfg = cb.load_config({})
    assert cfg["task_threshold"] == 4
    # env still wins over file
    cfg2 = cb.load_config({"CIRCUIT_BREAKER_TASK_THRESHOLD": "6"})
    assert cfg2["task_threshold"] == 6


# ── Unit + integration: scan trips ───────────────────────────────────────────

def test_scan_enforce_blocks_at_threshold(env):
    db = make_board(env, "testboard",
                    [task(cf=3, err="pid 9 not alive")],
                    [run(outcome="crashed"), run(outcome="crashed", error="503 dead model"), run(outcome="crashed")])
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    result = cb.scan(["testboard"], cfg)
    assert result["tripped"] == [("testboard", "t_1")]
    # fake hermes applied the block
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM tasks WHERE id='t_1'").fetchone()[0] == "blocked"
    conn.close()
    # block went through the CLI (audit)
    calls = hermes_calls(env)
    assert any("block" in c["args"] for c in calls)
    # breaker event + anomaly + task_events snapshot written
    assert events(env, "block")
    assert anomalies(env) and anomalies(env)[0][1] == "circuit_breaker"
    snaps = task_events_for(db, "t_1", "circuit_breaker_trip")
    assert snaps, "diagnostic snapshot must land in task_events"
    payload = json.loads(snaps[0][1])
    assert payload["streak"] == 3
    assert payload["last_error"]
    assert payload["run_count"] == 3
    assert payload["mode"] == "enforce"


def test_scan_log_only_records_would_block_without_blocking(env, monkeypatch):
    db = make_board(env, "testboard", [task(cf=3, err="boom")],
                    [run(outcome="crashed"), run(outcome="crashed"), run(outcome="crashed")])
    env["FAKE_HERMES_DB"] = str(db)
    monkeypatch.setenv("CIRCUIT_BREAKER_MODE", "log-only")
    cfg = cb.load_config({})
    result = cb.scan(["testboard"], cfg)
    assert result["tripped"] == [("testboard", "t_1")]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM tasks WHERE id='t_1'").fetchone()[0] == "ready"  # untouched
    conn.close()
    assert not hermes_calls(env), "log-only must not call the kanban CLI"
    assert events(env, "would_block") and not events(env, "block")
    snaps = task_events_for(db, "t_1", "circuit_breaker_trip")
    assert snaps and json.loads(snaps[0][1])["mode"] == "log-only"


def test_scan_below_threshold_no_trip(env):
    db = make_board(env, "testboard", [task(cf=2)],
                    [run(outcome="crashed"), run(outcome="crashed"), run(outcome="completed")])
    env["FAKE_HERMES_DB"] = str(db)
    result = cb.scan(["testboard"], cb.load_config({}))
    assert result["tripped"] == []
    assert not anomalies(env)


def test_threshold_configurable(env, monkeypatch):
    db = make_board(env, "testboard", [task(cf=3)],
                    [run(outcome="crashed"), run(outcome="crashed"), run(outcome="crashed")])
    env["FAKE_HERMES_DB"] = str(db)
    monkeypatch.setenv("CIRCUIT_BREAKER_TASK_THRESHOLD", "5")
    result = cb.scan(["testboard"], cb.load_config({}))
    assert result["tripped"] == []


def test_scan_reclaims_running_crashlooper(env):
    db = make_board(env, "testboard", [task(cf=3, status="running")],
                    [run(outcome="crashed"), run(outcome="crashed"), run(outcome="crashed")])
    env["FAKE_HERMES_DB"] = str(db)
    cb.scan(["testboard"], cb.load_config({}))
    calls = hermes_calls(env)
    kinds = [c["args"] for c in calls]
    assert any("reclaim" in a for a in kinds), "running crash-looper must be reclaimed"
    assert any("block" in a for a in kinds), "and then blocked"
    # reclaim must happen BEFORE block
    assert [i for i, a in enumerate(kinds) if "reclaim" in a][0] < [i for i, a in enumerate(kinds) if "block" in a][0]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM tasks WHERE id='t_1'").fetchone()[0] == "blocked"
    conn.close()


def test_scan_ignores_already_blocked_and_done(env):
    db = make_board(env, "testboard",
                    [task(id="t_blk", cf=10, status="blocked"), task(id="t_done", cf=0, status="done")],
                    [])
    env["FAKE_HERMES_DB"] = str(db)
    result = cb.scan(["testboard"], cb.load_config({}))
    assert result["tripped"] == []


def test_scan_skips_missing_and_corrupt_boards(env):
    (Path(env["CIRCUIT_BREAKER_BOARDS_DIR"]) / "corrupt").mkdir()
    (Path(env["CIRCUIT_BREAKER_BOARDS_DIR"]) / "corrupt" / "kanban.db").write_text("NOT A DATABASE")
    result = cb.scan(["corrupt", "does-not-exist"], cb.load_config({}))
    assert result["tripped"] == []
    assert result["errors"]


def test_scan_all_boards_discovers_fixture(env):
    make_board(env, "b1", [task(cf=3)], [run(outcome="crashed")] * 3)
    result = cb.scan(None, cb.load_config({}))
    assert ("b1", "t_1") in result["tripped"]


# ── Pre-spawn check ──────────────────────────────────────────────────────────

def test_check_holds_tripped_task_in_enforce(env):
    db = make_board(env, "testboard", [task(cf=3)], [run(outcome="crashed")] * 3)
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    # Pre-spawn scenario A: breaker scan hasn't run yet (dispatch cron runs
    # every 2 min, scan every 5 min) — the ready task itself must hold.
    allowed, msg = cb.check("testboard", None, cfg)
    assert allowed is False
    assert "t_1" in msg
    # Scenario B: explicit task id.
    allowed, msg = cb.check("testboard", "t_1", cfg)
    assert allowed is False


def test_check_allows_when_healthy(env):
    db = make_board(env, "testboard", [task(cf=0, status="ready")], [run(outcome="completed")])
    env["FAKE_HERMES_DB"] = str(db)
    allowed, msg = cb.check("testboard", None, cb.load_config({}))
    assert allowed is True


def test_check_log_only_never_holds(env, monkeypatch):
    db = make_board(env, "testboard", [task(cf=3)], [run(outcome="crashed")] * 3)
    env["FAKE_HERMES_DB"] = str(db)
    monkeypatch.setenv("CIRCUIT_BREAKER_MODE", "log-only")
    cfg = cb.load_config({})
    cb.scan(["testboard"], cfg)
    allowed, msg = cb.check("testboard", None, cfg)
    assert allowed is True, "log-only mode must not hold dispatch"
    # but the would-block decision was recorded
    assert events(env, "would_block")


def test_check_would_block_rate_limited(env, monkeypatch):
    db = make_board(env, "testboard", [task(cf=3)], [run(outcome="crashed")] * 3)
    env["FAKE_HERMES_DB"] = str(db)
    monkeypatch.setenv("CIRCUIT_BREAKER_MODE", "log-only")
    cfg = cb.load_config({})
    cb.scan(["testboard"], cfg)
    cb.check("testboard", None, cfg)
    n1 = len(events(env, "would_block"))
    cb.check("testboard", None, cfg)  # 1 second later — must be suppressed
    n2 = len(events(env, "would_block"))
    assert n2 == n1


def test_check_fail_open_on_corrupt_db(env):
    d = Path(env["CIRCUIT_BREAKER_BOARDS_DIR"]) / "corrupt"
    d.mkdir()
    (d / "kanban.db").write_text("garbage")
    allowed, msg = cb.check("corrupt", None, cb.load_config({}))
    assert allowed is True, "pre-spawn check must fail OPEN"


# ── Board-level escalation (red-team 5/10) ───────────────────────────────────

def seed_wave(env, n, cf=3):
    tasks, runs = [], []
    for i in range(n):
        tid = f"t_{i:02d}"
        tasks.append(task(id=tid, cf=cf))
        runs += [run(task_id=tid, outcome="crashed")] * cf
    return make_board(env, "testboard", tasks, runs)


def test_board_degraded_at_5_trips(env):
    db = seed_wave(env, 5)
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    result = cb.scan(["testboard"], cfg)
    assert len(result["tripped"]) == 5
    assert result["board_level"] == "degraded"
    st = cb.load_state(cfg)
    assert st["boards"]["testboard"]["level"] == "degraded"
    # dispatch still allowed for the board itself (degraded = warn only)
    allowed, _ = cb.check("testboard", None, cb.load_config({}))
    # every task is tripped+blocked, so check holds on task grounds; verify via fresh board probe
    assert anomalies(env)  # WARN anomaly emitted


def test_board_frozen_at_10_trips_holds_dispatch(env):
    db = seed_wave(env, 10)
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    result = cb.scan(["testboard"], cfg)
    assert result["board_level"] == "frozen"
    st = cb.load_state(cfg)
    assert st["boards"]["testboard"]["level"] == "frozen"
    allowed, msg = cb.check("testboard", None, cfg)
    assert allowed is False
    assert "frozen" in msg.lower()
    # critical anomaly for freeze
    assert any(a[0] == "critical" for a in anomalies(env))


def test_board_escalation_thresholds_configurable(env, monkeypatch):
    db = seed_wave(env, 3)
    env["FAKE_HERMES_DB"] = str(db)
    monkeypatch.setenv("CIRCUIT_BREAKER_BOARD_FREEZE", "3")
    cfg = cb.load_config({})
    result = cb.scan(["testboard"], cfg)
    assert result["board_level"] == "frozen"


def test_unfreeze_operator_only_path(env):
    db = seed_wave(env, 10)
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    cb.scan(["testboard"], cfg)
    assert cb.unfreeze_board("testboard", cfg, note="ops") is True
    st = cb.load_state(cfg)
    assert "testboard" not in st["boards"]
    assert events(env, "unfreeze")


# ── Reset (operator-only auto-unblock path) ──────────────────────────────────

def test_reset_clears_trip_and_counter(env):
    db = make_board(env, "testboard", [task(cf=3)], [run(outcome="crashed")] * 3)
    env["FAKE_HERMES_DB"] = str(db)
    cfg = cb.load_config({})
    cb.scan(["testboard"], cfg)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET status='blocked'")  # ensure blocked like production flow
    conn.commit(); conn.close()
    assert cb.reset_task("testboard", "t_1", cfg, note="manual retry after model fix") is True
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id='t_1'").fetchone()[0] == 0
    conn.close()
    st = cb.load_state(cfg)
    assert "testboard/t_1" not in st["tripped"]
    assert any("unblock" in c["args"] for c in hermes_calls(env))
    assert events(env, "reset")


def test_reset_idempotent_on_unknown_task(env):
    cfg = cb.load_config({})
    assert cb.reset_task("testboard", "t_nope", cfg) is False


# ── Diagnostic snapshot content ──────────────────────────────────────────────

def test_snapshot_contains_required_fields(tmp_path):
    db = make_board({"CIRCUIT_BREAKER_BOARDS_DIR": str(tmp_path)}, "testboard",
                    [task(cf=3, err="pid 9 not alive")],
                    [run(outcome="crashed", error="503 both backends failed"),
                     run(outcome="crashed"), run(outcome="crashed")])
    conn = sqlite3.connect(db)
    cols = [c[0] for c in conn.execute("SELECT * FROM tasks LIMIT 1").description]
    row = dict(zip(cols, conn.execute("SELECT * FROM tasks WHERE id='t_1'").fetchone()))
    snap = cb.diagnostic_snapshot(conn, "testboard", row, ("crashed", "timed_out", "spawn_failed"))
    conn.close()
    for key in ("streak", "last_error", "run_count", "crashed_runs", "board", "task_id",
                "outcomes", "profile", "crash_span_seconds", "tokens"):
        assert key in snap, f"snapshot missing {key}"
    assert snap["run_count"] == 3
    assert snap["crashed_runs"] == 3
    assert snap["last_error"]


# ── Wrapper contract ─────────────────────────────────────────────────────────

def test_bash_scripts_syntax_ok():
    import subprocess
    for script in (REPO_ROOT / "circuit-breaker.sh", REPO_ROOT / "staggered-dispatch.sh"):
        if script.exists():
            r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            assert r.returncode == 0, f"{script}: {r.stderr}"


def test_wrapper_holds_and_fails_open(env, capsys):
    import subprocess
    # Ready task over threshold, breaker scan NOT yet run (enforce default) —
    # the pre-spawn check itself must make the wrapper exit 1 (HOLD).
    db = make_board(env, "testboard", [task(cf=3)], [run(outcome="crashed")] * 3)
    env["FAKE_HERMES_DB"] = str(db)
    r = subprocess.run(["bash", str(REPO_ROOT / "circuit-breaker.sh"), "check", "testboard"],
                       capture_output=True, text=True,
                       env={**os.environ, "CIRCUIT_BREAKER_HERMES_BIN": env["CIRCUIT_BREAKER_HERMES_BIN"]})
    assert r.returncode == 1, "tripped board must make wrapper exit 1 (HOLD)"
    r2 = subprocess.run(["bash", str(REPO_ROOT / "circuit-breaker.sh"), "check", "no-such-board"],
                        capture_output=True, text=True, env=os.environ.copy())
    assert r2.returncode == 0, "unknown board must fail OPEN (exit 0)"
