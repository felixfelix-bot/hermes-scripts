"""Unit + integration tests for reap_stale_sessions.py.

Fixture strategy (mirrors tests/test_circuit_breaker.py):
- state.db files are built with the REAL production schema, copied verbatim
  from hermes-agent/hermes_state.py SCHEMA_SQL (sessions / messages /
  compression_locks + indexes). FTS tables/triggers are omitted: the reaper
  only UPDATEs the sessions table, which has no FTS triggers in production.
- All paths (profile roots, snapshot dirs) point into pytest tmp_path —
  no production file is ever touched.

Staleness contract under test (task t_44098319):
  stale  := sessions.ended_at IS NULL
            AND last_activity < cutoff
            AND no live (unexpired) compression_locks row
  last_activity := COALESCE(MAX(messages.timestamp), sessions.started_at)
  closing := UPDATE sessions SET ended_at=now, end_reason='reaped_stale'
             WHERE id=? AND ended_at IS NULL   (matches hermes_state.end_session:
             first end_reason wins; resume clears ended_at, so reaping is
             reversible via --rollback and non-destructive to messages).
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reap_stale_sessions as rs  # noqa: E402

# ── Real production DDL (verbatim from hermes-agent/hermes_state.py) ─────────
SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_source_id ON sessions(source, id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_compression_locks_expires ON compression_locks(expires_at);
"""

CUTOFF = "2026-08-12"  # task threshold
T_STALE_MSG = datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp()
T_RECENT = time.time() - 3600  # unambiguously after the cutoff
T_STARTED = datetime(2026, 8, 5, tzinfo=timezone.utc).timestamp()


def make_db(path, sessions=(), messages=(), locks=()):
    """Build a state.db with the production schema and seed rows."""
    con = sqlite3.connect(path)
    con.executescript(SESSIONS_DDL)
    for sid, started, ended, reason in sessions:
        con.execute(
            "INSERT INTO sessions (id, source, started_at, ended_at, end_reason, title)"
            " VALUES (?, 'cli', ?, ?, ?, ?)",
            (sid, started, ended, reason, f"title-{sid}"),
        )
    for sid, ts in messages:
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES"
            " (?, 'user', 'x', ?)",
            (sid, ts),
        )
    for sid, exp in locks:
        con.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at)"
            " VALUES (?, 'pid=1:test', ?, ?)",
            (sid, exp - 60, exp),
        )
    con.commit()
    con.close()
    return path


def get_session(db, sid):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    con.close()
    return row


@pytest.fixture
def db(tmp_path):
    return make_db(
        tmp_path / "state.db",
        sessions=[
            ("stale_sess", T_STARTED, None, None),          # stale: old message
            ("recent_sess", T_STARTED, None, None),         # active: recent message
            ("ended_sess", T_STARTED, T_STALE_MSG, "compression"),  # already closed
        ],
        messages=[("stale_sess", T_STALE_MSG), ("recent_sess", T_RECENT)],
    )


# ── selection ────────────────────────────────────────────────────────────────

def test_dry_run_default_makes_no_changes(db, tmp_path, capsys):
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF])
    assert rc == 0
    assert get_session(db, "stale_sess") == (None, None)  # untouched
    assert not any(tmp_path.rglob("*.json"))  # no snapshot written anywhere
    out = capsys.readouterr().out
    assert "stale_sess" in out and "DRY-RUN" in out


def test_execute_reaps_stale_and_writes_snapshot_first(db, tmp_path, capsys):
    snapdir = tmp_path / "snaps"
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(snapdir)])
    assert rc == 0

    ended, reason = get_session(db, "stale_sess")
    assert ended is not None and ended <= time.time()
    assert reason == "reaped_stale"

    snaps = list(snapdir.glob("*.json"))
    assert len(snaps) == 1
    snap = json.loads(snaps[0].read_text())
    row = next(r for r in snap["rows"] if r["id"] == "stale_sess")
    assert row["ended_at"] is None and row["end_reason"] is None  # prior state
    assert row["last_activity"] == T_STALE_MSG
    assert snap["cutoff"] == rs.parse_cutoff(CUTOFF)
    assert snap["end_reason"] == "reaped_stale"
    # recency guard: snapshot written BEFORE mutation (mtime <= ended_at)
    assert snaps[0].stat().st_mtime <= ended + 1

    out = capsys.readouterr().out
    assert "1" in out and "reaped" in out.lower()


def test_recent_session_never_touched(db):
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(db.parent / "s")])
    assert rc == 0
    assert get_session(db, "recent_sess") == (None, None)


def test_already_ended_session_excluded(db, tmp_path):
    snapdir = tmp_path / "snaps"
    rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
             "--snapshot-dir", str(snapdir)])
    # not re-reaped, marker not applied, original reason preserved
    assert get_session(db, "ended_sess") == (T_STALE_MSG, "compression")
    snap = json.loads(next(snapdir.glob("*.json")).read_text())
    assert all(r["id"] != "ended_sess" for r in snap["rows"])


def test_no_messages_falls_back_to_started_at(tmp_path):
    db = make_db(tmp_path / "state.db",
                 sessions=[("quiet", T_STARTED, None, None)])
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(tmp_path / "s")])
    assert rc == 0
    ended, reason = get_session(db, "quiet")
    assert ended is not None and reason == "reaped_stale"
    snap = json.loads(next((tmp_path / "s").glob("*.json")).read_text())
    assert snap["rows"][0]["last_activity"] == T_STARTED


def test_live_compression_lock_skipped(tmp_path):
    db = make_db(tmp_path / "state.db",
                 sessions=[("locked", T_STARTED, None, None)],
                 messages=[("locked", T_STALE_MSG)],
                 locks=[("locked", time.time() + 300)])
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(tmp_path / "s")])
    assert rc == 0
    assert get_session(db, "locked") == (None, None)


def test_expired_compression_lock_reaped(tmp_path):
    db = make_db(tmp_path / "state.db",
                 sessions=[("was_locked", T_STARTED, None, None)],
                 messages=[("was_locked", T_STALE_MSG)],
                 locks=[("was_locked", time.time() - 300)])
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(tmp_path / "s")])
    assert rc == 0
    assert get_session(db, "was_locked")[1] == "reaped_stale"


# ── idempotency + safety rails ───────────────────────────────────────────────

def test_idempotent_second_execute_reaps_zero(db, tmp_path, capsys):
    for _ in range(2):
        rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                      "--snapshot-dir", str(tmp_path / "s")])
        assert rc == 0
    out = capsys.readouterr().out
    assert "reaped=0" in out.splitlines()[-1] or "reaped: 0" in out


def test_future_cutoff_refused(db):
    rc = rs.main(["--db", str(db), "--cutoff", "2030-01-01", "--execute",
                  "--snapshot-dir", str(db.parent / "s")])
    assert rc != 0
    assert get_session(db, "stale_sess") == (None, None)  # nothing mutated


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_is_dry_run_by_default(db, tmp_path):
    snapdir = tmp_path / "snaps"
    rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
             "--snapshot-dir", str(snapdir)])
    snap = next(snapdir.glob("*.json"))
    rs.main(["--rollback", str(snap)])  # no --execute → restore must NOT happen
    assert get_session(db, "stale_sess")[1] == "reaped_stale"


def test_rollback_restores_prior_state(db, tmp_path):
    snapdir = tmp_path / "snaps"
    rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
             "--snapshot-dir", str(snapdir)])
    snap = next(snapdir.glob("*.json"))
    rc = rs.main(["--rollback", str(snap), "--execute"])
    assert rc == 0
    assert get_session(db, "stale_sess") == (None, None)


def test_rollback_spares_rows_reended_differently(db, tmp_path):
    snapdir = tmp_path / "snaps"
    rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
             "--snapshot-dir", str(snapdir)])
    snap = next(snapdir.glob("*.json"))
    # someone else legitimately ended it afterwards (first-end-wins violated by
    # a resume+end cycle): rollback must not clobber the newer end
    con = sqlite3.connect(db)
    con.execute("UPDATE sessions SET end_reason='compression' WHERE id='stale_sess'")
    con.commit()
    con.close()
    rs.main(["--rollback", str(snap), "--execute"])
    assert get_session(db, "stale_sess")[1] == "compression"


# ── multi-profile sweep ──────────────────────────────────────────────────────

def test_all_profiles_sweep(tmp_path):
    root = tmp_path / "profiles"
    for prof in ("alpha", "beta"):
        d = root / prof
        d.mkdir(parents=True)
        make_db(d / "state.db",
                sessions=[(f"{prof}_stale", T_STARTED, None, None)],
                messages=[(f"{prof}_stale", T_STALE_MSG)])
    (root / "not_a_profile_dir").mkdir()  # no state.db → skipped silently

    snapdir = tmp_path / "snaps"
    rc = rs.main(["--profiles-root", str(root), "--all-profiles",
                  "--cutoff", CUTOFF, "--execute", "--snapshot-dir", str(snapdir)])
    assert rc == 0
    for prof in ("alpha", "beta"):
        assert get_session(root / prof / "state.db", f"{prof}_stale")[1] == "reaped_stale"
    assert len(list(snapdir.glob("*.json"))) == 2  # one snapshot per profile


def test_requires_explicit_target(tmp_path):
    rc = rs.main(["--cutoff", CUTOFF])
    assert rc != 0  # refuse implicit sweeps of the real ~/.hermes


# ── hardening: race guard + sweep isolation ──────────────────────────────────

def test_execute_rechecks_staleness_at_write_time(tmp_path):
    # race guard: activity landing between the read (dry-run) and the write
    # (--execute) must spare the session
    db = make_db(tmp_path / "state.db",
                 sessions=[("raced", T_STARTED, None, None)],
                 messages=[("raced", T_STALE_MSG)])
    rs.main(["--db", str(db), "--cutoff", CUTOFF])  # read pass
    con = sqlite3.connect(db)  # user resumes right after the read
    con.execute("INSERT INTO messages (session_id, role, content, timestamp)"
                " VALUES ('raced', 'user', 'x', ?)", (time.time(),))
    con.commit()
    con.close()
    rc = rs.main(["--db", str(db), "--cutoff", CUTOFF, "--execute",
                  "--snapshot-dir", str(tmp_path / "s")])
    assert rc == 0
    assert get_session(db, "raced") == (None, None)  # resumed → spared


def test_sweep_continues_past_broken_profile(tmp_path):
    root = tmp_path / "profiles"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "state.db").write_bytes(b"this is not sqlite")
    d = root / "good"
    d.mkdir()
    make_db(d / "state.db",
            sessions=[("g_stale", T_STARTED, None, None)],
            messages=[("g_stale", T_STALE_MSG)])
    rc = rs.main(["--profiles-root", str(root), "--all-profiles",
                  "--cutoff", CUTOFF, "--execute", "--snapshot-dir", str(tmp_path / "s")])
    assert rc == 1  # partial failure is visible to cron
    assert get_session(d / "state.db", "g_stale")[1] == "reaped_stale"
