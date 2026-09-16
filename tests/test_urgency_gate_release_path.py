#!/usr/bin/env python3
"""Regression tests for urgency_gate.py's set/enrollment release path (CG-11).

Two bugs were found live 2026-09-16 while enrolling board ``bitcoin-node`` for
DEFER auto-dispatch:

1. ``cmd_set()`` guarded the write with ``if sql(board, w) is None: sys.exit``.
   ``sql()`` returns the CALLBACK's value and the write callback returned
   nothing, so a successful UPDATE reported ``db write failed`` (exit 1) and the
   eligibility unblock that follows was skipped.
2. ``tick()``'s enrolled branch did ``if n and n[0][0]`` on an already-fetched
   row, raising ``'int' object is not subscriptable`` for every enrolled board —
   swallowed by ``except Exception`` as "tick fail-open", so the per-board
   dispatch never ran and enrollment was silently a no-op.

Set ``UG_PATH`` to test a different revision of the module (used to prove the
suite is RED against the pre-fix file).

Run:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
UG_PATH = Path(os.environ.get("UG_PATH", HERE.parent / "urgency_gate.py"))


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def _load_module():
    spec = importlib.util.spec_from_file_location("urgency_gate_under_test", UG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Base(unittest.TestCase):
    """Temp board + stubbed out the network/CLI/nagging side effects."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "boards" / "b").mkdir(parents=True)
        self.db = self.root / "boards" / "b" / "kanban.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT,"
            " urgency TEXT, urgency_deadline INTEGER, urgency_set_at INTEGER,"
            " urgency_source TEXT, priority INTEGER DEFAULT 0,"
            " created_at INTEGER DEFAULT 0)"
        )
        conn.commit()
        conn.close()

        self.ug = _load_module()
        self.ug.BOARDS = str(self.root / "boards")
        self.ug.TIER_CACHE = str(self.root / "tier.json")
        self.ug.AUTODISPATCH = str(self.root / "autodispatch.txt")
        self.ug.LOG = str(self.root / "urgency-gate.log")
        self.ug.TRIAGE_TOUCH = str(self.root / "urgency-triage.touch")
        self.ug.touch_triage = lambda: None
        self.ug.log = lambda msg: None
        self.ug.alert = lambda msg: None

        # Record every CLI dispatch instead of running the real binary.
        self.calls = []

        def fake_kb(board, *args, **kwargs):
            self.calls.append((board,) + args)
            return _FakeProc()

        self.ug.kb = fake_kb
        self._tier = "expensive"
        self.ug.price_tier = lambda: {
            "ts": 0,
            "tier": self._tier,
            "evidence": ["test"],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def set_tier(self, tier):
        self._tier = tier

    def add_task(self, tid, status, urgency=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO tasks (id, title, status, urgency, priority, created_at)"
            " VALUES (?, ?, ?, ?, 0, 0)",
            (tid, tid, status, urgency),
        )
        conn.commit()
        conn.close()

    def task_row(self, tid):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT status, urgency, urgency_source FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        conn.close()
        return row

    def enroll(self, *boards):
        Path(self.ug.AUTODISPATCH).write_text("".join(f"{b}\n" for b in boards))

    def verbs(self):
        return [c[1] for c in self.calls]


class TestCmdSet(_Base):
    def test_set_stamps_urgency_and_reports_success(self):
        """cmd_set must not claim failure when the UPDATE committed."""
        self.add_task("t_1", "scheduled")
        try:
            rc = self.ug.cmd_set(["b", "t_1", "defer"])
        except SystemExit as exc:  # the bug: sys.exit("db write failed")
            self.fail(f"cmd_set exited on a successful write: {exc}")
        self.assertEqual(rc, 0)
        self.assertEqual(self.task_row("t_1"), ("scheduled", "defer", "operator"))

    def test_set_does_not_release_when_tier_is_ineligible(self):
        """DEFER at an expensive tier must stay parked."""
        self.add_task("t_1", "scheduled")
        self.set_tier("expensive")
        self.ug.cmd_set(["b", "t_1", "defer"])
        self.assertNotIn("unblock", self.verbs())
        self.assertEqual(self.task_row("t_1")[0], "scheduled")

    def test_set_releases_when_tier_is_eligible(self):
        self.add_task("t_1", "scheduled")
        self.set_tier("cheap")
        self.ug.cmd_set(["b", "t_1", "defer"])
        self.assertIn("unblock", self.verbs())


class TestTickEnrolledDispatch(_Base):
    def test_tick_dispatches_ready_work_on_enrolled_board(self):
        """Enrolled board with ready work => dispatch --max 1 (no TypeError)."""
        self.enroll("b")
        self.add_task("t_ready", "ready", "defer")
        self.ug.tick()
        self.assertIn(("b", "dispatch", "--max", "1"), self.calls)

    def test_tick_does_not_dispatch_unenrolled_board(self):
        self.add_task("t_ready", "ready", "defer")
        self.ug.tick()
        self.assertNotIn("dispatch", self.verbs())

    def test_tick_does_not_dispatch_when_nothing_is_ready(self):
        self.enroll("b")
        self.add_task("t_parked", "scheduled", "defer")
        self.ug.tick()
        self.assertNotIn("dispatch", self.verbs())

    def test_tick_holds_scheduled_defer_at_expensive_tier(self):
        self.enroll("b")
        self.add_task("t_parked", "scheduled", "defer")
        self.set_tier("expensive")
        self.ug.tick()
        self.assertNotIn("unblock", self.verbs())
        self.assertEqual(self.task_row("t_parked")[0], "scheduled")

    def test_tick_releases_scheduled_defer_at_cheap_tier(self):
        self.enroll("b")
        self.add_task("t_parked", "scheduled", "defer")
        self.set_tier("cheap")
        self.ug.tick()
        self.assertIn("unblock", self.verbs())


if __name__ == "__main__":
    unittest.main()
