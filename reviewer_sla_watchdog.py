#!/usr/bin/env python3
"""reviewer_sla_watchdog.py — reviewer-pool SLA watchdog (D-114, task t_55d56efa).

Scans a kanban board DB for review tasks that have exceeded the reviewer SLA,
and proposes a cross-family reassignment.

SLA contract (D-114 policy):
  stale := task assigned to a reviewer profile (worker-reviewer-kimi | glm),
           status IN ('running','review'), started_at set,
           now - started_at > sla_hours (default 4h).
  propose_reassign(a) := the OPPOSITE family profile (kimi <-> glm) per the
           D-114 cross-family pairing doctrine.
  politeness := a given task id alerts at most once per politeness window
           (default 6h), tracked by a JSON state file, so a chronically-stale
           review nags without spamming the 30-min cron tick.

Cron (silent watchdog contract): when there is nothing to report, stdout is
EMPTY — a script-only cron delivers nothing and stays quiet.

USAGE:
  reviewer_sla_watchdog.py --db <board-kanban.db>
      [--state-file <state.json>]   (default: next to --db, reviewer_sla_state.json)
      [--sla-hours 4]               (default 4)
      [--politeness-hours 6]        (default 6)
      [--max-reassign 1]            (default 1; beyond this -> route to manager)
      [--halt-file <path>]          (if present/non-empty: HALT; never reassign)
      [--dry-run]                   (report only; never mutates ledger)

Read-only on the board: this watchdog only READS the kanban DB and writes its
own state/ledger file. It never writes to the kanban DB. Reassignment is a
recommendation printed to stdout; the human/operator owns execution.

STATE / LEDGER (MANDATORY-HARDENED, consultant v3):
  The state file is a PERSISTENT ASSIGNMENT LEDGER that survives watchdog
  restarts. Per review task id it records:
      {task_id: {"last_alerted": <epoch>, "reassign_count": <int>}}
  reassign_count is the "reviewer-generation" used for dedup against the 4h
  merge-queue digest: both watchdog and digest may observe the same breach, but
  the dedup key (task_id, reassign_count) lets exactly one actor propose a given
  generation's reassignment. Cross-process persistence means decisions are made
  against durable state, never in-memory-only process state.

  Reassignment is capped at --max-reassign per task (default 1). After that the
  task is routed to the MANAGER queue instead (no kimi<->glm ping-pong). Under
  --halt-file the watchdog never reassigns — it logs HALT and routes to manager.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REVIEWER_PROFILES = ("worker-reviewer-kimi", "worker-reviewer-glm")
ACTIVE_STATUS = ("running", "review")

POLITENESS_H = 6  # default politeness window in hours


def now() -> float:
    """Seam for deterministic tests (monkeypatch wd.now)."""
    return time.time()


@dataclass(frozen=True)
class Alert:
    task_id: str
    title: str
    assignee: str
    age_h: float
    decision: str      # "reassign" => propose opposite family; "manager" => route to manager queue
    proposed_reassign: str  # filled only when decision == "reassign"


def _profile_family(profile: str) -> str:
    return "kimi" if profile.endswith("kimi") else "glm" if profile.endswith("glm") else ""


def propose_reassign(assignee: str) -> str:
    """D-114 cross-family pairing: kimi <-> glm."""
    family = _profile_family(assignee)
    if family == "kimi":
        return "worker-reviewer-glm"
    if family == "glm":
        return "worker-reviewer-kimi"
    raise ValueError(f"not a reviewer profile: {assignee}")


def scan(db_path: str, at: float, sla_hours: int = 4,
         max_reassign: int = 1, halt: bool = False,
         state: dict | None = None) -> list:
    """Return stale reviewer tasks from the board as Alert objects.

    Decision logic (consultant-hardened):
      - Under HALT (halt=True): never reassign — always decision='manager'.
      - Otherwise: if this task has already consumed max_reassign reassignments
        (per the persistent ledger's reassign_count), route to manager.
      - Else: decision='reassign' to the opposite family.
    `state` is optional for pure-scan callers; when omitted, reassign_count
    defaults to 0 (reassign).
    """
    state = state or {}
    q = (
        "SELECT id, title, assignee, status, started_at FROM tasks WHERE status = 'running' OR status = 'review'"
    )
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        con = sqlite3.connect(db_path)
    try:
        rows = con.execute(q).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass

    cutoff = at - sla_hours * 3600
    alerts = []
    for tid, title, assignee, _status, started in rows:
        if assignee not in REVIEWER_PROFILES:
            continue
        if not started:
            continue
        if started > cutoff:
            continue
        age_h = (at - started) / 3600.0
        rec = state.get(tid) or {}
        count = rec.get("reassign_count", 0) if isinstance(rec, dict) else 0
        if halt:
            decision, reassign = "manager", ""
        elif count >= max_reassign:
            decision, reassign = "manager", ""
        else:
            decision, reassign = "reassign", propose_reassign(assignee)
        alerts.append(
            Alert(
                task_id=tid,
                title=title or "",
                assignee=assignee,
                age_h=age_h,
                decision=decision,
                proposed_reassign=reassign,
            )
        )
    return alerts


def load_state(state_path: str) -> dict:
    """Load the persistent assignment ledger. Missing -> {}.

    Corrupt state is recovered to {} but logged loudly to stderr (never silent),
    because a silently-tossed ledger silently resets every reassign budget —
    the exact cap this watchdog exists to enforce (cold-review M2).
    """
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise ValueError("ledger is not a JSON object")
        return data
    except Exception as exc:
        print(f"WARN: reviewer_sla_watchdog: corrupt state file {state_path} "
              f"({exc}); recovered to empty ledger", file=sys.stderr)
        return {}


def _atomic_write(state_path: str, data: dict) -> None:
    """Write the ledger via temp-file + fsync + os.replace (new inode).

    This is atomic and durable: a reader holding the old fd never sees a torn
    file, and a mid-write kill leaves the previous version intact — never a
    truncated ledger that would silently reset the reassign budget
    (cold-review H1).
    """
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(data, indent=2)
    with open(tmp, "w") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def write_state(state_path: str, data: dict) -> None:
    _atomic_write(state_path, data)


class ledger_lock:
    """Advisory exclusive lock over the ledger file for the load->decide->write
    critical section.

    Guards against two overlapping cron invocations both reading count=0 and
    both proposing the same reassignment (cold-review H2). Uses the lock file
    itself + fcntl.flock; the target file need not exist yet because flock is on
    an open fd of the lock file we create.

    USAGE:
        with ledger_lock(state_path):
            state = load_state(state_path)
            # decide ...
            write_state(state_path, new_state)
    """

    def __init__(self, state_path: str):
        self.lockfile = str(Path(state_path).with_suffix(Path(state_path).suffix + ".lock"))
        self._fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:  # non-POSIX fallback: no-op lock
            return self
        self._fh = open(self.lockfile, "a")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


def scan_after_politeness(db_path: str, at: float, state: dict,
                          sla_hours: int = 4, politeness_h: int = POLITENESS_H,
                          max_reassign: int = 1, halt: bool = False) -> list:
    """Filter scanned alerts through the politeness window + decision logic."""
    alerts = scan(db_path, at, sla_hours, max_reassign=max_reassign,
                  halt=halt, state=state)
    window_s = politeness_h * 3600
    fresh = []
    for a in alerts:
        rec = state.get(a.task_id)
        if isinstance(rec, dict):
            last = rec.get("last_alerted")
        else:
            last = rec
        if last is None or (at - float(last)) >= window_s:
            fresh.append(a)
    return fresh


def render(alerts: list) -> str:
    """Render alerts as human-readable lines; empty list -> ''."""
    if not alerts:
        return ""
    lines = ["REVIEWER SLA EXCEEDED — action required:"]
    for a in alerts:
        if a.decision == "reassign":
            lines.append(
                f"- {a.task_id} ({a.title[:60]}) | {a.assignee} | "
                f"{a.age_h:.1f}h stale | REASSIGN -> {a.proposed_reassign}"
            )
        else:
            lines.append(
                f"- {a.task_id} ({a.title[:60]}) | {a.assignee} | "
                f"{a.age_h:.1f}h stale | ROUTE TO MANAGER (reassign budget exhausted)"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reviewer-pool SLA watchdog (D-114)")
    ap.add_argument("--db", required=True, help="board kanban.db path")
    ap.add_argument("--state-file", default=None, help="politeness state JSON (default: next to db)")
    ap.add_argument("--sla-hours", type=float, default=4.0, help="SLA threshold in hours (default 4)")
    ap.add_argument("--politeness-hours", type=float, default=float(POLITENESS_H),
                    help="no-realert window in hours (default 6)")
    ap.add_argument("--max-reassign", type=int, default=1,
                    help="max reassign proposals per task before routing to manager (default 1)")
    ap.add_argument("--halt-file", default=None,
                    help="if this path exists non-empty, HALT: never reassign; route to manager")
    ap.add_argument("--dry-run", action="store_true", help="report only; do NOT persist the ledger")

    args = ap.parse_args(argv)

    db_path = args.db
    if not Path(db_path).exists():
        print(f"ERROR: board db not found: {db_path}", file=sys.stderr)
        return 1

    state_path = args.state_file or str(Path(db_path).parent / "reviewer_sla_state.json")
    at = now()

    halt = False
    if args.halt_file:
        hp = Path(args.halt_file)
        halt = hp.exists() and hp.read_text(errors="ignore").strip() != ""

    # Serialize the whole load->decide->write critical section so overlapping
    # cron invocations cannot both read count=0 and double-propose (H2).
    with ledger_lock(state_path):
        state = load_state(state_path)

        alerts = scan_after_politeness(
            db_path, at, state,
            sla_hours=args.sla_hours, politeness_h=args.politeness_hours,
            max_reassign=args.max_reassign, halt=halt,
        )

        # Persistent assignment ledger update (dedup key = (task_id,
        # reassign_count)): only when we actually report (not dry-run, and there
        # is something to report).
        out = render(alerts)
        if out:
            print(out)
            if not args.dry_run:
                new_state = dict(state)
                for a in alerts:
                    rec = new_state.get(a.task_id)
                    if not isinstance(rec, dict):
                        rec = {}  # type: ignore[assignment]
                        new_state[a.task_id] = rec
                    rec["last_alerted"] = at
                    if a.decision == "reassign":
                        rec["reassign_count"] = rec.get("reassign_count", 0) + 1
                    # HALT / manager-routed alerts do NOT consume the budget.
                write_state(state_path, new_state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
