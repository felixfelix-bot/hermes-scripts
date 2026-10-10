#!/usr/bin/env python3
"""RED/GREEN regression for the fleet-offload re-block loop (t_00fa8726).

Symptom filed 2026-09-13: fleet_scheduler kept writing
``BLOCKED: fleet-offload:<node>`` comments to the SAME cards every ~2.6 minutes,
~4000 comments in 3h across ~20 boards. Field forensics (task_events, meshcore):

  t_a8fcf378  blocked(cobrador)@1791541710 -> unblocked@1791552087
              -> blocked(cobrador)@1791552428 -> block_loop_detected(recurrences=2)
              -> card ARCHIVED@1791552565
  t_67f3b03e  blocked(cobrador)@1791541864 -> block_loop_detected -> promoted
              -> blocked(cobrador)@1791591185 -> block_loop_detected(recurrences=3)

Two independent holes, both proven by the tests below:

  A. release_stale_holds() drops a hold WITHOUT installing the ``hold_denied``
     guard whenever the card is not currently blocked/scheduled (the card was
     unblocked or promoted by another writer). apply_holds() then re-blocks on
     the very next tick — the exact behaviour its own docstring forbids
     ("the winner is denied until it publishes a fresh fleet-running heartbeat,
     so the next tick cannot immediately re-block the card we just freed").

  B. Nothing consults the board DB before ``kanban block``, so when the volatile
     state entry loses ``held`` (purge / _prune_state / scheduler restart) a card
     that already carries our recent hold gets a second BLOCKED comment, and the
     kernel's block-loop detector then ARCHIVES the card.

Run: python3 tests/test_fleet_scheduler_hold_loop.py   (exit 0 = all green)
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

# Test the copy in THIS tree (never ~/.hermes/scripts — the live file is the
# thing being fixed, and testing it would make RED/GREEN meaningless).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fleet_scheduler as fs  # noqa: E402

BOARD = "meshcore"
TTL = 1800
NOW = 1_800_000_000.0


class Kernel:
    """Minimal stand-in for `hermes kanban block|unblock` + the board DB."""

    def __init__(self, tmp: Path):
        self.hermes = tmp
        self.db = tmp / "kanban" / "boards" / BOARD / "kanban.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db)
        c.executescript(
            "create table tasks (id text primary key, status text, assignee text,"
            " current_run_id text, worker_pid integer);"
            "create table task_comments (id integer primary key autoincrement,"
            " task_id text, author text, body text, created_at integer);"
        )
        c.commit()
        c.close()
        self.calls: list[list[str]] = []

    # -- DB helpers -------------------------------------------------------
    def _conn(self):
        return sqlite3.connect(self.db)

    def add_task(self, tid: str, status: str = "ready", assignee: str = "worker-base"):
        c = self._conn()
        c.execute("insert into tasks (id,status,assignee) values (?,?,?)",
                  (tid, status, assignee))
        c.commit()
        c.close()

    def set_status(self, tid: str, status: str):
        c = self._conn()
        c.execute("update tasks set status=? where id=?", (status, tid))
        c.commit()
        c.close()

    def status(self, tid: str) -> str:
        c = self._conn()
        row = c.execute("select status from tasks where id=?", (tid,)).fetchone()
        c.close()
        return str(row[0]) if row else ""

    def comment(self, tid: str, body: str, ts: float, author: str = "manager"):
        c = self._conn()
        c.execute("insert into task_comments (task_id,author,body,created_at)"
                  " values (?,?,?,?)", (tid, author, body, int(ts)))
        c.commit()
        c.close()

    def comments(self, tid: str, like: str = "%BLOCKED: fleet-offload%"):
        c = self._conn()
        rows = c.execute("select body from task_comments where task_id=? and body like ?",
                         (tid, like)).fetchall()
        c.close()
        return [r[0] for r in rows]

    # -- CLI stand-in -----------------------------------------------------
    def __call__(self, args: list[str], now: float = NOW) -> tuple[int, str]:
        self.calls.append(list(args))
        if "block" in args:
            i = args.index("block")
            tid, reason = args[i + 1], args[i + 2]
            if self.status(tid) not in ("ready", "running"):
                return 1, f"cannot block task in status {self.status(tid)!r}"
            c = self._conn()
            c.execute("insert into task_comments (task_id,author,body,created_at)"
                      " values (?,?,?,?)", (tid, "manager", f"BLOCKED: {reason}",
                                            int(now)))
            c.execute("update tasks set status='blocked' where id=?", (tid,))
            c.commit()
            c.close()
            return 0, "blocked"
        if "unblock" in args:
            i = args.index("unblock")
            tid = args[i + 1]
            if self.status(tid) not in ("blocked", "scheduled"):
                return 1, "not blocked"
            self.set_status(tid, "ready")
            return 0, "unblocked"
        return 0, ""

    def block_calls(self, tid: str) -> int:
        return sum(1 for a in self.calls if "block" in a and tid in a)


def install(tmp: Path) -> Kernel:
    """Point fleet_scheduler at a throwaway HERMES home + fake kanban CLI."""
    k = Kernel(tmp)
    fs.HERMES = tmp
    fs.BOT = tmp / "bot"
    fs._hold_enabled = lambda: True          # type: ignore[assignment]
    fs._hold_ttl = lambda: TTL               # type: ignore[assignment]
    fs._has_local_run = lambda info, now: False   # type: ignore[assignment]

    def cli(args, _k=k):
        return _k(args)

    fs._kanban = cli                          # type: ignore[assignment]
    return k


def entry(tid: str, held=None, held_ts=None) -> dict:
    info = {"ts": int(NOW) - 4000, "board": BOARD, "task": tid, "held": held,
            "transport": "public", "assignee": "worker-base", "tags": []}
    if held_ts is not None:
        info["held_ts"] = held_ts
    return info


def key(tid: str) -> str:
    return f"{BOARD}:{tid}"


def run_ticks(state: dict, winners: dict, ticks: int, step: float = 60.0,
              each_tick=None):
    """One scheduler tick = release_stale_holds() then apply_holds()."""
    for i in range(ticks):
        now = NOW + i * step
        if each_tick is not None:
            each_tick(i, now)
        fs.release_stale_holds(state, now)
        fs.apply_holds(state, winners, now, peers=None)


# ---------------------------------------------------------------------------
# A. a hold released for a non-acking winner must not be re-blocked
# ---------------------------------------------------------------------------
def test_released_hold_is_not_reblocked_for_a_dark_winner():
    """The card was unblocked by another writer; the scheduler must not re-block.

    RED before the fix: release_stale_holds() drops the hold on the
    'status is not blocked/scheduled' branch without recording the deny, so
    apply_holds() re-blocks immediately and the kernel writes a second
    BLOCKED comment.
    """
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_dark1"
        k.add_task(tid, "ready")                       # promoted/unblocked elsewhere
        state = {"advertised": {key(tid): entry(tid, held="dq05", held_ts=NOW - 4000)},
                 "running": {}, "hold_denied": {}}

        run_ticks(state, {key(tid): "dq05"}, ticks=1)   # TTL expired -> release path

        assert k.comments(tid) == [], (
            f"re-blocked a card that was just released for a dark winner: "
            f"{k.comments(tid)}")
        assert state["hold_denied"].get(tid, {}).get("dq05"), (
            "released hold left no deny guard, so the next tick can re-block")


def test_dark_winner_loop_is_bounded_over_many_ticks():
    """Field cadence: another writer promotes the card back to ready, and the
    hold TTL keeps expiring.

    RED before the fix: every expiry releases the hold without the deny guard
    and re-blocks -> one BLOCKED comment per TTL period, i.e. the firehose.
    """
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_dark2"
        k.add_task(tid, "ready")
        state = {"advertised": {key(tid): entry(tid, held="dq05", held_ts=NOW - 4000)},
                 "running": {}, "hold_denied": {}}

        def promoter(i, now):
            # whatever else unblocks/promotes the card between ticks
            if k.status(tid) == "blocked":
                k.set_status(tid, "ready")

        run_ticks(state, {key(tid): "dq05"}, ticks=24, step=TTL / 3.0,
                  each_tick=promoter)

        n = len(k.comments(tid))
        assert n <= 1, f"{n} BLOCKED comments over 24 ticks (re-block firehose)"


# ---------------------------------------------------------------------------
# B. never write a second BLOCKED comment for the same winner
# ---------------------------------------------------------------------------
def test_holdable_card_with_a_recent_hold_is_adopted_not_recommented():
    """`held` was lost from the state entry, but the card still carries our hold.

    RED before the fix: apply_holds() calls `kanban block` again -> a second
    comment for the SAME winner, which trips block_loop_detected (t_67f3b03e
    was promoted back to ready and re-blocked for cobrador 1390s later).
    """
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_dup1"
        k.add_task(tid, "ready")                       # promoted/re-opened
        k.comment(tid, "BLOCKED: fleet-offload:cobrador", ts=NOW - 300)
        state = {"advertised": {key(tid): entry(tid)}, "running": {}, "hold_denied": {}}

        run_ticks(state, {key(tid): "cobrador"}, ticks=4)

        assert k.comments(tid) == ["BLOCKED: fleet-offload:cobrador"], (
            f"duplicate BLOCKED comment for the same winner: {k.comments(tid)}")
        assert k.block_calls(tid) == 0, "issued a redundant `kanban block`"
        assert state["advertised"][key(tid)]["held"] == "cobrador", (
            "the existing hold was not adopted")


def test_blocked_card_is_not_blocked_again():
    """A card already blocked by our hold must never be re-commented."""
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_dup2"
        k.add_task(tid, "blocked")
        k.comment(tid, "BLOCKED: fleet-offload:cobrador", ts=NOW - 60)
        state = {"advertised": {key(tid): entry(tid)}, "running": {}, "hold_denied": {}}

        run_ticks(state, {key(tid): "cobrador"}, ticks=3)

        assert k.comments(tid) == ["BLOCKED: fleet-offload:cobrador"]
        assert k.status(tid) == "blocked"


def test_historical_hold_comment_does_not_suppress_a_real_hold():
    """Dedupe must be bounded: the September backlog is not re-adopted.

    A stale `BLOCKED: fleet-offload:` comment older than the TTL must not stop
    the scheduler from holding a card that is ready again for a live winner.
    """
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_old1"
        k.add_task(tid, "ready")
        k.comment(tid, "BLOCKED: fleet-offload:cobrador", ts=NOW - 10 * TTL)
        state = {"advertised": {key(tid): entry(tid)}, "running": {}, "hold_denied": {}}

        n = fs.apply_holds(state, {key(tid): "cobrador"}, NOW, peers=None)

        assert n == 1, "a stale block comment suppressed a legitimate hold"
        assert len(k.comments(tid)) == 2
        assert k.status(tid) == "blocked"


def test_block_for_a_different_winner_still_happens():
    """Dedupe is per-winner: a new winner re-holds the card."""
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_dup3"
        k.add_task(tid, "ready")
        k.comment(tid, "BLOCKED: fleet-offload:dq05", ts=NOW - 300)
        state = {"advertised": {key(tid): entry(tid)}, "running": {}, "hold_denied": {}}

        n = fs.apply_holds(state, {key(tid): "cobrador"}, NOW, peers=None)

        assert n == 1, "a card held for dq05 must still be holdable for cobrador"
        assert k.status(tid) == "blocked"


def test_fresh_ready_card_is_still_held():
    """Regression guard: the ordinary hold path must keep working."""
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_new1"
        k.add_task(tid, "ready")
        state = {"advertised": {key(tid): entry(tid)}, "running": {}, "hold_denied": {}}

        n = fs.apply_holds(state, {key(tid): "cobrador"}, NOW, peers=None)

        assert n == 1, "a queued card for a peer was not held"
        assert k.status(tid) == "blocked"
        assert k.comments(tid) == ["BLOCKED: fleet-offload:cobrador"]
        assert state["advertised"][key(tid)]["held"] == "cobrador"


def test_released_hold_for_a_live_winner_is_still_holdable():
    """The deny guard must not disarm the feature: a winner that heartbeats
    (fleet-running for this task) is allowed to hold again.
    """
    with tempfile.TemporaryDirectory() as d:
        k = install(Path(d))
        tid = "t_live1"
        k.add_task(tid, "ready")
        state = {"advertised": {key(tid): entry(tid)},
                 "running": {key(tid): {"node": "cobrador", "ts": NOW - 30}},
                 "hold_denied": {tid: {"cobrador": NOW - 10}}}

        n = fs.apply_holds(state, {key(tid): "cobrador"}, NOW, peers=None)

        assert n == 1, "a winner with a fresh heartbeat was still denied"
        assert k.status(tid) == "blocked"
        assert not state["hold_denied"][tid].get("cobrador")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                fails += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if fails else 0)
