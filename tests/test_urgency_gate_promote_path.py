#!/usr/bin/env python3
"""Regression tests for urgency_gate.py's ``gate_promote`` path (CG-12).

Two bugs found live 2026-10-06 while trying to bump the PCB-gate card
``t_0f39b353`` from ``defer`` to ``soon`` and promote it out of triage:

1. ``gate_promote()`` flattened the ids TWICE. The SQL lambda already did
   ``[i for (i,) in c.execute(...)]``, so line 273's second
   ``ok = [i for (i,) in (ok or [])]`` unpacked each *string* id into one
   element: ``ValueError: too many values to unpack (expected 1)`` — raised for
   any real card id that HAD been classified, i.e. exactly the promote path the
   gate exists to serve. ``hermes kanban promote <t_id>`` died with a traceback.
2. With no ``t_``-prefixed id in argv the ``IN (%s)`` clause rendered as
   ``IN ()`` — a SQL syntax error swallowed by ``sql()``'s fail-open, surfacing
   as the misleading ``SQL fail-open (default): unable to open database file``.

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
    spec = importlib.util.spec_from_file_location("urgency_gate_promote_under_test", UG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Base(unittest.TestCase):
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

        self.execd = []      # argv handed to the real CLI
        self.parks = []      # (tid, reason) park() calls

        def fake_run_real(argv, *a, **k):
            self.execd.append(list(argv))
            return _FakeProc()

        self.ug.run_real = fake_run_real
        self.ug.park = lambda board, tid, reason, *a, **k: self.parks.append((tid, reason))

    def tearDown(self):
        self.tmp.cleanup()

    def add_task(self, tid, status, urgency=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO tasks (id, title, status, urgency, priority, created_at)"
            " VALUES (?, ?, ?, ?, 0, 0)",
            (tid, tid, status, urgency),
        )
        conn.commit()
        conn.close()


class TestGatePromote(_Base):
    def test_promote_passes_a_classified_card_to_the_real_cli(self):
        """A classified card must reach `kanban promote <id>` — no ValueError."""
        self.add_task("t_1", "triage", "soon")
        try:
            rc = self.ug.gate_promote(["--board", "b", "t_1"])
        except ValueError as exc:  # the bug: double flatten
            self.fail(f"gate_promote raised on a classified card: {exc}")
        self.assertEqual(rc, 0)
        self.assertIn(["kanban", "--board", "b", "promote", "t_1"], self.execd)

    def test_promote_passes_every_classified_card(self):
        self.add_task("t_1", "triage", "soon")
        self.add_task("t_2", "triage", "now")
        self.ug.gate_promote(["--board", "b", "t_1", "t_2"])
        self.assertEqual(
            [a for a in self.execd if a[:1] == ["kanban"]],
            [["kanban", "--board", "b", "promote", "t_1", "t_2"]],
        )

    def test_promote_refuses_an_unclassified_card(self):
        """Unclassified work stays parked; the gate must fail closed."""
        self.add_task("t_9", "triage", None)
        rc = self.ug.gate_promote(["--board", "b", "t_9"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.execd, [])
        self.assertTrue(any(t == "t_9" for t, _ in self.parks))

    def test_promote_with_no_ids_is_a_clean_noop(self):
        """No `t_` id => no SQL, no crash, no CLI call."""
        try:
            rc = self.ug.gate_promote(["--board", "b"])
        except Exception as exc:
            self.fail(f"gate_promote raised with no ids: {exc!r}")
        self.assertEqual(rc, 0)
        self.assertEqual(self.execd, [])


if __name__ == "__main__":
    unittest.main()
