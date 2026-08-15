#!/usr/bin/env python3
"""circuit_breaker.py — D3 crash circuit-breaker for the Hermes kanban fleet.

Stops the crash-loop token bleed (858M tokens / 48h, ~86% non-productive runs,
see ~/api_burn_report_48h.md). A task whose last N dispatched runs all crashed
("crashed"/"timed_out"/"spawn_failed") is tripped: reclaimed if running,
blocked so no dispatcher re-spawns it, given a diagnostic snapshot in the
board's task_events, and surfaced to the operator through the existing
anomaly_events → daemon_metrics pending-alerts → anomaly-notify.sh chain.

Design (decided 2026-08-15, handover P1 / red-team):
  - Home: kanban-DB level, NOT proxy-side. The proxy's 503-burst detection
    (rate_limit_gate.py, T3.1) sees transport failures per key/model; it cannot
    see task semantics (gave_up vs crashed vs completed) nor per-task streaks.
    This breaker reads the boards — the only place "3 consecutive crashed
    runs" is knowable — and is complementary: dead-model 503 storms are T3.1's
    job; crash-loops that survive transport (context re-feed pathology,
    iteration-budget exhaustion, workspace errors) are this breaker's job.
  - Threshold: 3 consecutive crash-family runs per task (backtest 2026-08-15:
    trips on 94 tasks, prevents ~1,120 re-dispatched runs, 93% precision,
    12 false positives — most completed on manual retry, covered by --reset).
  - Board-level escalation (red-team): >=5 trips within 24h => board degraded
    (WARN anomaly); >=10 => board frozen (CRITICAL anomaly, dispatch held).
  - Auto-unblock policy: NONE. Operator-only via `reset` / `unfreeze`.
  - Staged rollout: mode log-only (records would-block decisions, never
    blocks) first, then flip CIRCUIT_BREAKER_MODE=enforce.
  - Fail-open everywhere: a broken/missing breaker can never wedge dispatch
    (same contract as rate_limit_gate_check.sh).

Exit codes (check): 0 = allow, 1 = HOLD, 2 = internal error (callers treat
any non-1 as allow; circuit-breaker.sh maps this).

Usage:
  circuit_breaker.py scan [--boards b1,b2]     # breaker-daemon pass (cron 5min)
  circuit_breaker.py check <board> [task_id]   # pre-spawn check (exit 0/1)
  circuit_breaker.py reset <board> <task_id> [--note ...]   # operator unblock
  circuit_breaker.py unfreeze <board> [--note ...]
  circuit_breaker.py status [--json]
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.0.0"

DEFAULTS = {
    "mode": "log-only",              # log-only | enforce
    "task_threshold": 3,             # consecutive crash-family runs to trip
    "board_warn_threshold": 5,       # trips in window => board degraded
    "board_freeze_threshold": 10,    # trips in window => board frozen
    "crash_outcomes": ["crashed", "timed_out", "spawn_failed"],
    "scan_window_hours": 24,         # window for board-level escalation
    "would_block_ratelimit_s": 1800, # min seconds between would_block logs per task
    "skip_boards": ["default", "archive", "archived"],
    "hermes_bin": os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
    "boards_dir": os.path.expanduser("~/.hermes/kanban/boards"),
    "state_file": os.path.expanduser("~/.hermes/state/circuit_breaker.json"),
    "usage_db": os.path.expanduser("~/.hermes/bot/zai_usage.db"),
    "config_file": os.path.expanduser("~/.hermes/config/circuit-breaker.json"),
}

ENV_OVERRIDES = {
    "mode": "CIRCUIT_BREAKER_MODE",
    "task_threshold": "CIRCUIT_BREAKER_TASK_THRESHOLD",
    "board_warn_threshold": "CIRCUIT_BREAKER_BOARD_WARN",
    "board_freeze_threshold": "CIRCUIT_BREAKER_BOARD_FREEZE",
    "hermes_bin": "CIRCUIT_BREAKER_HERMES_BIN",
    "boards_dir": "CIRCUIT_BREAKER_BOARDS_DIR",
    "state_file": "CIRCUIT_BREAKER_STATE",
    "usage_db": "CIRCUIT_BREAKER_USAGE_DB",
    "config_file": "CIRCUIT_BREAKER_CONFIG",
}

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    board TEXT, task_id TEXT,
    action TEXT NOT NULL,   -- would_block|block|reclaim|board_degraded|board_frozen|reset|unfreeze|error
    streak INTEGER, mode TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_cb_events_ts ON circuit_breaker_events(ts);
CREATE INDEX IF NOT EXISTS idx_cb_events_task ON circuit_breaker_events(board, task_id, action);
"""

ANOMALY_DDL = """
CREATE TABLE IF NOT EXISTS anomaly_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    severity  TEXT NOT NULL,
    category  TEXT NOT NULL,
    title     TEXT,
    detail    TEXT,
    alerted   INTEGER DEFAULT 0,
    resolved  INTEGER DEFAULT 0
);
"""


# ── Config / state ────────────────────────────────────────────────────────────

def load_config(env=None):
    env = env if env else os.environ  # empty/None -> read process env
    cfg = dict(DEFAULTS)
    # config file (lowest precedence above defaults)
    path = env.get("CIRCUIT_BREAKER_CONFIG") or cfg["config_file"]
    try:
        if path and os.path.isfile(path):
            with open(path) as f:
                file_cfg = json.load(f)
            if isinstance(file_cfg, dict):
                cfg.update({k: v for k, v in file_cfg.items() if k in DEFAULTS})
    except Exception:
        pass  # fail-open: broken config file => defaults
    # env overrides
    for key, var in ENV_OVERRIDES.items():
        if env.get(var):
            val = env[var]
            if key in ("task_threshold", "board_warn_threshold", "board_freeze_threshold"):
                try:
                    val = int(val)
                except ValueError:
                    continue
            cfg[key] = val
    return cfg


def load_state(cfg=None):
    path = (cfg or {}).get("state_file", DEFAULTS["state_file"])
    try:
        with open(path) as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("tripped", {})
            st.setdefault("boards", {})
            return st
    except Exception:
        pass
    return {"tripped": {}, "boards": {}}


def save_state(state, cfg):
    path = cfg["state_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


# ── Board DB helpers ──────────────────────────────────────────────────────────

def board_db_path(board, cfg):
    return Path(cfg["boards_dir"]) / board / "kanban.db"


def open_board(board, cfg, readonly=True):
    """Open a board DB; returns (conn|None, error|None). Never raises."""
    db = board_db_path(board, cfg)
    if not db.exists():
        return None, f"no board db at {db}"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro" if readonly else f"file:{db}", uri=True)
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()  # corruption probe
        return conn, None
    except Exception as e:
        return None, f"board {board} unreadable: {e}"


TASK_COLS = ("id", "title", "status", "assignee", "consecutive_failures",
             "last_failure_error", "created_at", "model_override")


def fetch_tasks(conn, statuses=("ready", "running", "todo")):
    q = f"SELECT {', '.join(TASK_COLS)} FROM tasks WHERE status IN ({','.join('?' * len(statuses))})"
    rows = conn.execute(q, statuses).fetchall()
    return [dict(zip(TASK_COLS, r)) for r in rows]


def computed_streak(conn, task_id, crash_outcomes):
    """Trailing run-level crash streak from task_runs (newest first)."""
    rows = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC", (task_id,)).fetchall()
    streak = 0
    for (outcome,) in rows:
        if outcome in crash_outcomes:
            streak += 1
        else:
            break  # completed/blocked/reclaimed/... breaks the streak
    return streak


def task_streak(conn, task_row, crash_outcomes):
    """max(kernel-maintained column, computed from runs). Column also counts
    spawn failures that may not leave run rows; runs catch a stale column."""
    col = task_row.get("consecutive_failures") or 0
    return max(int(col), computed_streak(conn, task_row["id"], crash_outcomes))


def diagnostic_snapshot(conn, board, task_row, crash_outcomes, mode=None):
    """Evidence bundle written to task_events on trip (and would-block)."""
    tid = task_row["id"]
    runs = conn.execute(
        "SELECT id, profile, outcome, started_at, ended_at, error FROM task_runs "
        "WHERE task_id=? ORDER BY started_at ASC, id ASC", (tid,)).fetchall()
    crashed = [r for r in runs if r[2] in crash_outcomes]
    last_err = task_row.get("last_failure_error")
    if not last_err and crashed:
        last_err = crashed[-1][5]
    first_ts = crashed[0][4] if crashed else None
    last_ts = crashed[-1][4] if crashed else None
    snap = {
        "breaker": "D3 crash circuit-breaker",
        "version": VERSION,
        "board": board,
        "task_id": tid,
        "title": task_row.get("title"),
        "profile": task_row.get("assignee"),
        "status_at_trip": task_row.get("status"),
        "streak": task_streak(conn, task_row, crash_outcomes),
        "last_error": last_err,
        "run_count": len(runs),
        "crashed_runs": len(crashed),
        "outcomes": [r[2] for r in runs][-10:],
        "crash_span_seconds": (last_ts - first_ts) if (first_ts and last_ts) else None,
        "model_override": task_row.get("model_override"),
        # Token attribution is not yet possible per-task (api_calls has no
        # session column — see productivity-gate-design.md §1.4). Recorded as
        # null honestly until session-id linkage ships; crash_span + run_count
        # bound the burn in the meantime.
        "tokens": None,
    }
    if mode:
        snap["mode"] = mode
    return snap


# ── Usage-DB audit + alerting (existing operator chain) ─────────────────────

def usage_conn(cfg):
    os.makedirs(os.path.dirname(cfg["usage_db"]), exist_ok=True)
    conn = sqlite3.connect(cfg["usage_db"])
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(EVENTS_DDL)
    conn.executescript(ANOMALY_DDL)
    return conn


def log_event(cfg, action, board=None, task_id=None, streak=None, mode=None, detail=None):
    try:
        conn = usage_conn(cfg)
        conn.execute(
            "INSERT INTO circuit_breaker_events (ts, board, task_id, action, streak, mode, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), board, task_id, action, streak, mode,
             json.dumps(detail) if detail is not None else None))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"WARN: breaker event log write failed: {e}", file=sys.stderr)


def raise_anomaly(cfg, severity, title, detail):
    """Write into the existing anomaly_events table — picked up within 5 min by
    anomaly-notify.sh via daemon_metrics.py pending-alerts (dedup on
    severity+title gives escalating backoff, not spam)."""
    try:
        conn = usage_conn(cfg)
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail, alerted, resolved) "
            "VALUES (?,?,?,?,?,0,0)",
            (time.time(), severity, "circuit_breaker", title, detail))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"WARN: anomaly write failed: {e}", file=sys.stderr)


def write_task_event(cfg, board, task_id, kind, payload):
    """Append a structured event to the board's own task_events (audit where
    the operator already looks). Opens its own WRITABLE connection — scan runs
    with a read-only handle."""
    try:
        db = board_db_path(board, cfg)
        conn = sqlite3.connect(f"file:{db}", uri=True, timeout=10)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (task_id, None, kind, json.dumps(payload), int(time.time())))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"WARN: task_events write failed: {e}", file=sys.stderr)


# ── Kanban CLI actions (the sanctioned mutation path) ────────────────────────

def run_hermes(cfg, args, timeout=20):
    try:
        r = subprocess.run([cfg["hermes_bin"]] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def kanban(cfg, board, *args):
    return run_hermes(cfg, ["kanban", "--board", board] + list(args))


# ── Trip / reset / freeze ─────────────────────────────────────────────────────

def trip_task(cfg, state, board, task_row, snap, conn):
    """Apply breaker action for one task. Returns action taken."""
    mode = cfg["mode"]
    tid = task_row["id"]
    enforce = mode == "enforce"
    action = "block" if enforce else "would_block"

    if enforce:
        if task_row["status"] == "running":
            ok, out = kanban(cfg, board, "reclaim", tid, "--reason",
                             f"circuit-breaker: {snap['streak']} consecutive crashed runs")
            log_event(cfg, "reclaim", board, tid, snap["streak"], mode, {"cli_out": out[:400]})
            if not ok:
                log_event(cfg, "error", board, tid, snap["streak"], mode, {"step": "reclaim", "out": out[:400]})
        reason = (f"circuit-breaker (D3): {snap['streak']} consecutive crashed runs — "
                  f"last error: {str(snap['last_error'])[:200]}")
        ok, out = kanban(cfg, board, "block", tid, reason)
        if not ok:
            log_event(cfg, "error", board, tid, snap["streak"], mode, {"step": "block", "out": out[:400]})

    snap["would_block"] = not enforce
    write_task_event(cfg, board, tid, "circuit_breaker_trip", snap)
    log_event(cfg, action, board, tid, snap["streak"], mode, snap)
    raise_anomaly(cfg, "warning",
                  f"Circuit breaker: {board}/{tid} ({snap['streak']} consecutive crashed runs)",
                  json.dumps({k: snap.get(k) for k in
                              ("last_error", "run_count", "crashed_runs", "profile", "status_at_trip")}))
    state["tripped"][f"{board}/{tid}"] = {
        "ts": time.time(), "streak": snap["streak"],
        "last_error": snap["last_error"], "mode": mode,
    }


def trip_threshold_hits(cfg, state):
    """Distinct tasks tripped inside the scan window (board escalation input)."""
    cutoff = time.time() - cfg["scan_window_hours"] * 3600
    try:
        conn = usage_conn(cfg)
        rows = conn.execute(
            "SELECT DISTINCT board, task_id FROM circuit_breaker_events "
            "WHERE action IN ('block','would_block') AND ts >= ?", (cutoff,)).fetchall()
        conn.close()
        return {(b, t) for b, t in rows}
    except Exception:
        return set()


def escalate_board(cfg, state, board, tripped_now):
    """Red-team 5/10 ladder: 5 trips/24h => degraded (WARN); 10 => frozen
    (CRITICAL + dispatch hold). Degraded never holds dispatch by itself."""
    hits = trip_threshold_hits(cfg, state)
    n = len({t for (b, t) in hits if b == board} | set(tripped_now))
    level = None
    if n >= cfg["board_freeze_threshold"]:
        level = "frozen"
    elif n >= cfg["board_warn_threshold"]:
        level = "degraded"
    cur = state["boards"].get(board, {}).get("level")
    if level == "frozen" and cur != "frozen":
        state["boards"][board] = {"level": "frozen", "since": time.time(), "trips_24h": n}
        log_event(cfg, "board_frozen", board=board, mode=cfg["mode"], detail={"trips_24h": n})
        raise_anomaly(cfg, "critical",
                      f"Circuit breaker: board '{board}' FROZEN ({n} tripped tasks in 24h)",
                      "Dispatch held for this board. Operator unfreeze: "
                      "circuit_breaker.py unfreeze " + board)
        return "frozen"
    if level == "degraded" and cur not in ("degraded", "frozen"):
        state["boards"][board] = {"level": "degraded", "since": time.time(), "trips_24h": n}
        log_event(cfg, "board_degraded", board=board, mode=cfg["mode"], detail={"trips_24h": n})
        raise_anomaly(cfg, "warning",
                      f"Circuit breaker: board '{board}' degraded ({n} tripped tasks in 24h)",
                      "Approaching freeze threshold. Inspect crash causes.")
        return "degraded"
    return cur


# ── Commands ──────────────────────────────────────────────────────────────────

def scan(boards, cfg):
    """One breaker-daemon pass. Returns summary dict; never raises."""
    state = load_state(cfg)
    result = {"tripped": [], "errors": [], "board_level": None, "scanned": 0}
    if boards is None:
        bdir = Path(cfg["boards_dir"])
        boards = []
        if bdir.is_dir():
            boards = sorted(d.name for d in bdir.iterdir()
                            if d.is_dir() and d.name not in cfg["skip_boards"])
    for board in boards:
        if board in cfg["skip_boards"]:
            continue
        conn, err = open_board(board, cfg)
        if conn is None:
            result["errors"].append(f"{board}: {err}")
            continue
        try:
            tripped_now = []
            for t in fetch_tasks(conn):
                key = f"{board}/{t['id']}"
                if key in state["tripped"]:
                    continue
                streak = task_streak(conn, t, cfg["crash_outcomes"])
                if streak < cfg["task_threshold"]:
                    continue
                snap = diagnostic_snapshot(conn, board, t, cfg["crash_outcomes"], mode=cfg["mode"])
                trip_task(cfg, state, board, t, snap, conn)
                result["tripped"].append((board, t["id"]))
                tripped_now.append(t["id"])
            if tripped_now:
                lvl = escalate_board(cfg, state, board, tripped_now)
                if lvl:
                    result["board_level"] = lvl
            result["scanned"] += 1
        except Exception as e:
            result["errors"].append(f"{board}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    save_state(state, cfg)
    return result


def next_ready_task(conn):
    row = conn.execute(
        "SELECT id, consecutive_failures FROM tasks WHERE status='ready' "
        "ORDER BY priority DESC, created_at ASC LIMIT 1").fetchone()
    return row


def check(board, task_id, cfg):
    """Pre-spawn check. Returns (allowed: bool, message). FAIL-OPEN on errors."""
    state = load_state(cfg)
    enforce = cfg["mode"] == "enforce"
    bstate = state["boards"].get(board, {})
    frozen = bstate.get("level") == "frozen"
    if frozen:
        if enforce:
            return False, f"board {board} FROZEN by circuit breaker ({bstate.get('trips_24h')} trips/24h)"
        _log_would_block(cfg, board, task_id or "*", "board_frozen")

    conn, err = open_board(board, cfg)
    if conn is None:
        return True, f"fail-open: {err}"  # unknown/corrupt board => allow
    try:
        if not task_id:
            row = next_ready_task(conn)
            if row is None:
                return True, "no ready tasks"
            task_id, _cf = row
        key = f"{board}/{task_id}"
        if key in state["tripped"]:
            if enforce:
                return False, f"task {task_id} tripped ({state['tripped'][key]['streak']} consecutive crashes)"
            _log_would_block(cfg, board, task_id, "task_tripped")
            return True, f"log-only: would block {task_id}"
        # not yet in state (scan lag): decide from the DB directly
        cols = ("id", "title", "status", "assignee", "consecutive_failures",
                "last_failure_error", "created_at", "model_override")
        row = conn.execute(f"SELECT {', '.join(cols)} FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return True, "task not found"
        t = dict(zip(cols, row))
        streak = task_streak(conn, t, cfg["crash_outcomes"])
        if streak >= cfg["task_threshold"]:
            if enforce:
                return False, f"task {task_id} at {streak} consecutive crashes (threshold {cfg['task_threshold']})"
            _log_would_block(cfg, board, task_id, "task_over_threshold")
            return True, f"log-only: would block {task_id} (streak {streak})"
        return True, "ok"
    except Exception as e:
        return True, f"fail-open: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _log_would_block(cfg, board, task_id, reason):
    """Rate-limited would-block logging for log-only mode (scan cron + the
    every-2-min dispatch checks must not spam the event log)."""
    try:
        conn = usage_conn(cfg)
        cutoff = time.time() - cfg["would_block_ratelimit_s"]
        row = conn.execute(
            "SELECT MAX(ts) FROM circuit_breaker_events "
            "WHERE board=? AND task_id=? AND action='would_block'", (board, task_id)).fetchone()
        conn.close()
        if row and row[0] and row[0] >= cutoff:
            return
    except Exception:
        pass
    log_event(cfg, "would_block", board, task_id, mode="log-only", detail={"reason": reason})


def reset_task(board, task_id, cfg, note=None):
    """Operator-only unblock (the breaker NEVER auto-unblocks)."""
    conn, err = open_board(board, cfg, readonly=False)
    if conn is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return False
    row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if row is None:
        return False
    state = load_state(cfg)
    state["tripped"].pop(f"{board}/{task_id}", None)
    save_state(state, cfg)
    ok, out = kanban(cfg, board, "unblock", task_id, "--reason",
                     f"circuit-breaker reset{' — ' + note if note else ''}")
    if not ok:
        print(f"WARN: unblock CLI failed: {out}", file=sys.stderr)
    # zero the kernel counter so the dispatcher's own failure limit + this
    # breaker don't immediately re-trip a task the operator chose to retry
    conn, _ = open_board(board, cfg, readonly=False)
    if conn is not None:
        try:
            conn.execute("UPDATE tasks SET consecutive_failures=0 WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
    log_event(cfg, "reset", board, task_id, mode=cfg["mode"], detail={"note": note})
    print(f"RESET {board}/{task_id}: unblocked, counter zeroed, breaker state cleared")
    return True


def unfreeze_board(board, cfg, note=None):
    state = load_state(cfg)
    if board not in state["boards"]:
        return False
    del state["boards"][board]
    save_state(state, cfg)
    log_event(cfg, "unfreeze", board=board, mode=cfg["mode"], detail={"note": note})
    print(f"UNFROZE board {board}")
    return True


def status(cfg, as_json=False):
    state = load_state(cfg)
    out = {
        "version": VERSION,
        "mode": cfg["mode"],
        "thresholds": {
            "task": cfg["task_threshold"],
            "board_warn": cfg["board_warn_threshold"],
            "board_freeze": cfg["board_freeze_threshold"],
        },
        "tripped_tasks": len(state["tripped"]),
        "boards": state["boards"],
    }
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        print(f"circuit-breaker v{VERSION} mode={cfg['mode']}")
        print(f"  task threshold: {cfg['task_threshold']} consecutive crash-family runs")
        print(f"  board escalation: warn@{cfg['board_warn_threshold']} freeze@{cfg['board_freeze_threshold']} "
              f"per {cfg['scan_window_hours']}h window")
        print(f"  tripped tasks: {len(state['tripped'])}")
        for key, v in list(state["tripped"].items())[:20]:
            print(f"    {key}: streak={v.get('streak')} ({v.get('mode')})")
        print(f"  board states: {json.dumps(state['boards']) or '{}'}")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="D3 crash circuit-breaker")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")
    ps = sub.add_parser("scan", help="breaker-daemon pass over boards")
    ps.add_argument("--boards", help="comma-separated board list (default: all)")
    pc = sub.add_parser("check", help="pre-spawn check (exit 0 allow / 1 HOLD)")
    pc.add_argument("board")
    pc.add_argument("task_id", nargs="?", default=None)
    pr = sub.add_parser("reset", help="operator-only: unblock a tripped task")
    pr.add_argument("board")
    pr.add_argument("task_id")
    pr.add_argument("--note", default=None)
    pu = sub.add_parser("unfreeze", help="operator-only: unfreeze a board")
    pu.add_argument("board")
    pu.add_argument("--note", default=None)
    pst = sub.add_parser("status", help="show breaker state")
    pst.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    cfg = load_config()

    if args.cmd == "scan":
        boards = args.boards.split(",") if args.boards else None
        r = scan(boards, cfg)
        print(json.dumps(r))
        return 0
    if args.cmd == "check":
        allowed, msg = check(args.board, args.task_id, cfg)
        print(msg)
        return 0 if allowed else 1
    if args.cmd == "reset":
        return 0 if reset_task(args.board, args.task_id, cfg, note=args.note) else 1
    if args.cmd == "unfreeze":
        return 0 if unfreeze_board(args.board, cfg, note=args.note) else 1
    if args.cmd == "status":
        status(cfg, as_json=args.json)
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
