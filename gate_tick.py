#!/usr/bin/env python3
"""gate_tick.py — scheduled tiered gate enforcement (D-128).

Scans tasks that are `done` and evaluates their completion evidence with
gate_engine. code-tier tasks missing a required gate are blocked (hard) with an
explanatory comment; docs-tier tasks are flagged (advisory) only. Idempotent per
task via a state file. Empty stdout except when something changed (Hermes cron
--no-agent friendly).

Usage:
  gate_tick.py [--board B ...] [--since-min N] [--report] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_engine as ge  # type: ignore

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS = HERMES / "kanban" / "boards"
STATE = HERMES / "bot" / "gate_tick_state.json"


def _boards(slugs):
    if slugs:
        return [BOARDS / s for s in slugs]
    return sorted(p.parent for p in BOARDS.glob("*/kanban.db"))


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def _comment(conn, tid, body):
    conn.execute(
        "insert into task_comments (task_id, author, body, created_at) "
        "values (?,?,?,?)", (tid, "gate-tick", body, int(time.time())))


CI_HELPER = "~/.hermes/profiles/manager/scripts/ngit_ci_evidence.py"


def _ci_note(res):
    """Clear text when the ci_evidence gate is what is missing (D-128 extension)."""
    if "ci_evidence" not in (res.get("missing") or []):
        return ""
    if res.get("ci_absence_documented"):
        return (" CI evidence (ci_evidence): a documented CI absence was recorded but "
                "is NOT a pass for the code tier — obtain the live ngit CI result for "
                f"the exact head under review ({CI_HELPER} <repo> --commit <sha>).")
    return (" CI evidence (ci_evidence) is required: cite the live ngit CI workflow + "
            "conclusion for the exact head under review "
            f"({CI_HELPER} <repo> --commit <sha>). 'no results' (exit 2) is not green; "
            "a documented absence is not accepted for the code tier.")


def tick(slugs, since_min, gates, report=False):
    cutoff = int(time.time()) - since_min * 60
    state = _load()
    changes = []
    for bdir in _boards(slugs):
        db = bdir / "kanban.db"
        if not db.exists():
            continue
        board = bdir.name
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "select id from tasks where status='done' and "
                "(completed_at is null or completed_at>=?)", (cutoff,)).fetchall()
            for (tid,) in rows:
                key = f"{board}:{tid}"
                res = ge.evaluate_task(board, tid, gates)
                if res.get("spec_error") or "gate_spec" in (res.get("missing") or []):
                    # No usable spec is an INFRASTRUCTURE fault, not a task
                    # defect: never flip a card to blocked because the spec
                    # went missing. main() reports it and exits non-zero.
                    continue
                verdict = res.get("verdict")
                prev = state.get(key)
                if verdict in (None, "pass"):
                    continue
                if prev == verdict:
                    continue
                missing = ", ".join(res.get("missing", []))
                ci_note = _ci_note(res)
                if verdict == "block" and not report:
                    _comment(conn, tid,
                             f"gate-tick: BLOCKED — missing gate(s): {missing}. "
                             f"tier={res.get('tier')} "
                             f"author_family={res.get('author_family')}. "
                             "Resolve and re-run to pass."
                             f"{ci_note}")
                    conn.execute("update tasks set status='blocked' where id=?", (tid,))
                else:
                    _comment(conn, tid,
                             f"gate-tick: advisory — missing gate(s): {missing} "
                             f"(tier={res.get('tier')}).{ci_note}")
                conn.commit()
                state[key] = verdict
                changes.append(res)
        finally:
            conn.close()
    if changes and not report:
        _save(state)
    return changes


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="append", dest="boards")
    ap.add_argument("--since-min", type=int, default=1440)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    gates = ge.load_gates()
    # FAIL CLOSED: with no usable spec nothing can be enforced, so the tick
    # must not touch the board and must not exit 0 — a silent "green because
    # nothing ran" is exactly the failure this gate exists to prevent.
    if gates.get("_spec_error"):
        print(f"[gate-tick] SPEC ERROR — refusing to run: {gates['_spec_error']}")
        print(f"[gate-tick] spec paths tried: {gates.get('_spec_tried')}")
        return 2
    changes = tick(args.boards, args.since_min, gates, report=args.report)
    if args.json:
        print(json.dumps(changes, indent=1))
    elif changes:
        for c in changes:
            print(f"[gate-tick] {c.get('board')}/{c.get('id')} "
                  f"tier={c.get('tier')} verdict={c.get('verdict')} "
                  f"missing={c.get('missing')} spec={c.get('spec_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
