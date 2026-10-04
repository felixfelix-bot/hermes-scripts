#!/usr/bin/env python3
"""task-lifecycle-governor.py — auto-revive crash-loop blocked tasks with backoff.

Phase 7 of the token-bleed recovery. Consumes the SAME headroom signal as the
dispatch governor so revival never adds resource pressure.

Three tiers (in order of cost):
  1. Die-after-3-tries is UNCHANGED (circuit-breaker / failure_limit block).
  2. Deterministic auto-revive (zero tokens): classify each `blocked` task;
     clearly `crash-loop` ones (iteration-budget / consecutive-failure) are
     auto-revived with a doubling cooldown (1h→2h→4h→… cap 24h), budget raise,
     and failure reset — ONLY when the Kalman headroom governor reports idle
     resources.
  3. Escalate to the hourly consultant for `dependency` / `needs-input` /
     `sticky` and crash-loops whose backoff hit the cap.

Surfaces a `task-lifecycle` anomaly (reclaimed / revived / still-blocked counts)
via the existing anomaly_events → anomaly-notify.sh → Signal pipeline.

Fail-open: any error logs and exits 0 (never wedges dispatch).

Usage:
  task-lifecycle-governor.py scan [--dry-run]   # cron pass
  task-lifecycle-governor.py status             # print backlog summary
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERMES = HOME / ".hermes"
BOT = HERMES / "bot"
STATE_FILE = BOT / "task_lifecycle_state.json"
USAGE_DB = BOT / "zai_usage.db"
BOARDS = HERMES / "kanban" / "boards"

# Backoff schedule (seconds). A crash-loop task is revived after this cooldown
# since its last revival; each revival doubles the cooldown up to the cap.
BACKOFF_STEPS = [3600, 7200, 14400, 28800, 57600, 86400]  # 1h,2h,4h,8h,16h,24h
BACKOFF_CAP = 86400  # 24h
# Budget raise applied on revival.
GOAL_MAX_TURNS = 160
MAX_RUNTIME_SECONDS = 5400

# Hard cap on crash-loop revives. The block<->revive ping-pong — a crash-loop
# card re-readied every cooldown while the dispatcher/guard re-blocks it — is
# what silently burned worker slots (2026-09-22: `CARD LOOP ... burning worker
# slots`). After this many revives a still-crashing card stays blocked and is
# escalated instead of revived again. Override: TASK_REVIVE_MAX.
MAX_REVIVES = int(os.environ.get("TASK_REVIVE_MAX", "3"))
# Block reasons that mean a guard/breaker parked the card on purpose. Reviving
# one undoes that park, so it must never happen automatically.
GUARD_MARKERS = (
    "fleet_loop_guard", "reclaim loop", "reclaim backoff", "dispatcher guard",
    "loop-guard", "circuit breaker", "auto-blocked",
)

# Headroom gate: only revive when the dispatch governor reports idle resources.
HEADROOM_FILE = BOT / "dispatch_headroom.json"

# Preventive cleanup (Phase 8B): when a resource enters the WARNING band
# (below the governor's hard-hold threshold), create a house-keeping cleanup
# task at urgency=now so pressure never reaches the dispatch-blocking point.
CLEANUP_BOARD = "house-keeping"
CLEANUP_WARN = {
    "disk_used_pct": 78.0,     # hold is 90; warn well before
    "memory_pct": 78.0,        # hold is 85
    "swap_used_pct": 70.0,     # hold is 80
}
HERMES_BIN = str(HERMES / "hermes-agent" / "venv" / "bin" / "hermes")
HERMES_PY = str(HERMES / "hermes-agent" / "venv" / "bin" / "python3")
URGENCY_GATE = str(HERMES / "scripts" / "urgency_gate.py")

# Plebeian review/fix boards (operator directive 2026-09-11): every review task
# and every reviewer-requested-change task must be urgency=now so the dispatcher
# picks it up the moment resources free up. The urgency gate parks unclassified
# tasks; this sweeper classifies them and releases anything parked so the queues
# never stall waiting for an urgency stamp.
PLEBEIAN_URGENCY_BOARDS = ("plebeian", "plebeian-my-prs", "plebeian-pr-reviews")


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _headroom_ok() -> bool:
    """True when the dispatch governor reports idle resources (can dispatch).

    Reads the headroom state the gateway dispatcher computes. If the file is
    absent (governor not yet writing it), fall back to a conservative check of
    the resource Kalman so we don't revive into a starved box.
    """
    try:
        if HEADROOM_FILE.exists():
            data = json.loads(HEADROOM_FILE.read_text(encoding="utf-8"))
            return bool(data.get("can_dispatch", True))
    except Exception:
        pass
    # Fallback: quick memory/load check.
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            p = line.split()
            if len(p) >= 2:
                info[p[0].rstrip(":")] = int(p[1])
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        load = float(Path("/proc/loadavg").read_text().split()[0])
        nproc = int(subprocess.check_output(["nproc"], text=True).strip())
        return avail > 500 and load < 2.0 * nproc
    except Exception:
        return True  # fail-open


def _classify(task) -> str:
    """Classify a blocked task: crash-loop | dependency | needs-input | sticky."""
    bk = (task.get("block_kind") or "").lower()
    lfe = (task.get("last_failure_error") or "").lower()
    if bk == "needs_input":
        return "needs-input"
    if bk == "capability":
        return "sticky"
    if "budget exhausted" in lfe or "iteration" in lfe or "not alive" in lfe:
        return "crash-loop"
    if bk == "dependency":
        return "dependency"
    # Default: crash-loop if it has failures, else sticky (needs human judgment).
    if int(task.get("consecutive_failures") or 0) > 0:
        return "crash-loop"
    return "sticky"


def _revive(conn, tid, reason):
    """Raise budget + reset failures + unblock a crash-loop task."""
    conn.execute(
        "UPDATE tasks SET goal_max_turns=?, max_runtime_seconds=?, "
        "consecutive_failures=0, status='ready', block_kind=NULL, "
        "last_failure_error=NULL WHERE id=?",
        (GOAL_MAX_TURNS, MAX_RUNTIME_SECONDS, tid),
    )
    conn.commit()


def _guard_block_reason(conn, tid, task) -> str:
    """First guard/breaker marker found in the task's block reason, else ''.

    Scans the task columns *and* recent block/reclaim events (the CLI block
    reason lives in the event payload, not always in ``last_failure_error``).
    Never raises.
    """
    hay = f"{task.get('block_kind') or ''} {task.get('last_failure_error') or ''}".lower()
    try:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind IN "
            "('block_loop_detected','blocked','reclaimed','gave_up') "
            "ORDER BY created_at DESC LIMIT 3", (tid,)
        ).fetchall()
        hay += " " + " ".join(str(r[0] or "") for r in rows).lower()
    except Exception:
        pass
    for marker in GUARD_MARKERS:
        if marker in hay:
            return marker
    return ""


def _revive_decision(guard_reason: str, count: int,
                     max_revives: int = MAX_REVIVES) -> str:
    """Pure decision: 'revive' | 'guard-parked' | 'capped'.

    Guard-parked cards are never auto-revived; crash-loops that already burned
    ``max_revives`` slots are capped so the block<->revive ping-pong stops.
    """
    if guard_reason:
        return "guard-parked"
    if count >= max_revives:
        return "capped"
    return "revive"


def _notify(severity, title, detail):
    try:
        conn = sqlite3.connect(str(USAGE_DB), timeout=5)
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail) "
            "VALUES (?, ?, 'task-lifecycle', ?, ?)",
            (time.time(), severity, title, detail),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def scan(dry_run=False):
    state = _load_state()
    now = time.time()
    revived = []
    still_blocked = {"crash-loop": 0, "dependency": 0, "needs-input": 0, "sticky": 0}
    escalated = []
    needs_input_list = []  # (board, id, title) — surfaced so they actually reach the operator

    # Only revive when resources are idle (headroom gate).
    headroom_ok = _headroom_ok()

    for db in sorted(BOARDS.glob("*/kanban.db")):
        board = db.parent.name
        try:
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, block_kind, last_failure_error, "
                "consecutive_failures, goal_max_turns, max_runtime_seconds "
                "FROM tasks WHERE status='blocked'"
            ).fetchall()
            for row in rows:
                task = dict(row)
                cat = _classify(task)
                tid = task["id"]
                if cat == "crash-loop":
                    # Backoff: how many times has this task been revived?
                    key = f"revive_count:{tid}"
                    count = int(state.get(key, 0))
                    cooldown = BACKOFF_STEPS[min(count, len(BACKOFF_STEPS) - 1)]
                    last = float(state.get(f"last_revive:{tid}", 0))
                    guard_reason = _guard_block_reason(conn, tid, task)
                    decision = _revive_decision(guard_reason, count)
                    if decision != "revive":
                        # Never undo a guard/breaker park, and stop reviving a
                        # card that already burned MAX_REVIVES worker slots.
                        still_blocked["crash-loop"] += 1
                        escalated.append((board, tid, task["title"]))
                    elif now - last >= cooldown:
                        if headroom_ok:
                            if not dry_run:
                                _revive(conn, tid, "crash-loop backoff")
                                state[key] = count + 1
                                state[f"last_revive:{tid}"] = now
                            revived.append((board, tid, count + 1))
                        else:
                            still_blocked["crash-loop"] += 1
                    else:
                        still_blocked["crash-loop"] += 1
                        # Escalate to consultant once backoff hits the cap.
                        if count >= len(BACKOFF_STEPS) - 1:
                            escalated.append((board, tid, task["title"]))
                else:
                    still_blocked[cat] += 1
                    if cat == "needs-input":
                        needs_input_list.append((board, tid, (task.get("title") or "")[:70]))
            conn.close()
        except Exception:
            continue

    _save_state(state)

    # Surface via anomaly_events (dedup'd by anomaly-notify.sh).
    if revived or any(still_blocked.values()):
        detail = (
            f"revived={len(revived)} "
            f"still_blocked={json.dumps(still_blocked)} "
            f"escalated_to_consultant={len(escalated)} "
            f"headroom_ok={headroom_ok}"
        )
        sev = "info" if revived else "warning"
        _notify(sev, "task-lifecycle: backlog sweep", detail)

    # Human-input blockers MUST reach the operator (they otherwise sit on the
    # board forever — the [human-gate-timeout] escalation never surfaces).
    if needs_input_list:
        lines = "; ".join(f"{b}/{i}: {t}" for b, i, t in needs_input_list[:8])
        _notify("warning", "task-lifecycle: tasks NEED INPUT",
                f"{len(needs_input_list)} blocked awaiting operator input → {lines}")

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] headroom_ok={headroom_ok} "
        f"revived={len(revived)} still_blocked={json.dumps(still_blocked)} "
        f"escalated={len(escalated)}"
    )
    return 0


def _open_cleanup_task_exists(board, resource):
    """True if an open cleanup task for this resource already exists (dedup)."""
    db = BOARDS / board / "kanban.db"
    if not db.exists():
        return False
    try:
        c = sqlite3.connect(str(db))
        n = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','archived') "
            "AND title LIKE ?", (f"%Preventive cleanup%{resource}%",)
        ).fetchone()[0]
        c.close()
        return n > 0
    except Exception:
        return False


def preventive_cleanup(dry_run=False):
    """Create house-keeping cleanup tasks when a resource nears its hold threshold.

    Phase 8B: the Kalman/governor early-warning fires at the WARNING band
    (below the dispatch-hold threshold) so cleanup happens before dispatch is
    ever gated. Deduped: one open cleanup task per resource.
    """
    metrics = {}
    try:
        c = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True)
        row = c.execute(
            "SELECT cpu_load_1m, memory_used_percent, swap_used_percent, "
            "disk_used_percent FROM resource_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        c.close()
        if row:
            metrics = {"cpu_load_1m": row[0], "memory_pct": row[1],
                       "swap_used_pct": row[2], "disk_used_pct": row[3]}
    except Exception:
        pass

    created = []
    for resource, threshold in CLEANUP_WARN.items():
        val = metrics.get(resource)
        if val is None or float(val) < threshold:
            continue
        if _open_cleanup_task_exists(CLEANUP_BOARD, resource):
            continue
        title = f"Preventive cleanup: {resource} at {float(val):.0f}% (warn {threshold:.0f}%)"
        body = (
            f"Resource {resource} is at {float(val):.0f}% (warning threshold "
            f"{threshold:.0f}%). Run a cleanup pass before it reaches the "
            "dispatch-hold threshold. Known hogs: ~/.tmp (stale dirs), /tmp "
            "scratch, orphaned worktrees/, state.db-wal, snap old revisions, "
            "old logs, regenerable caches."
        )
        if dry_run:
            created.append(title)
            continue
        try:
            # Create with an idempotency key (built-in dedup) + stamp urgency=now
            # (this CLI has no --urgency flag; urgency lives in a column set by
            # the urgency gate, and an unstamped task gets parked by the gate).
            ikey = f"preventive-cleanup-{resource}"
            r = subprocess.run(
                [HERMES_BIN, "kanban", "--board", CLEANUP_BOARD, "create", title,
                 "--assignee", "worker-admin", "--priority", "100",
                 "--idempotency-key", ikey, "--body", body, "--json"],
                capture_output=True, text=True, timeout=60,
            )
            tid = ""
            try:
                tid = (json.loads(r.stdout or "{}") or {}).get("id", "")
            except Exception:
                tid = ""
            if tid:
                subprocess.run(
                    [HERMES_PY, URGENCY_GATE, "set", CLEANUP_BOARD, tid, "now"],
                    capture_output=True, text=True, timeout=60,
                )
            created.append(title)
        except Exception:
            pass

    if created:
        _notify("warning", "task-lifecycle: preventive cleanup",
                f"created {len(created)} cleanup task(s): {created}")
    print(f"[{datetime.now(timezone.utc).isoformat()}] preventive cleanup created={len(created)}")
    return 0


def orphan_worktree_gc(dry_run=False):
    """Find worktree dirs with no live task reference and surface them (8C.4)."""
    wt_root = HOME / "worktrees"
    if not wt_root.is_dir():
        return 0
    # Collect all live (non-terminal) workspace_paths across boards.
    live = set()
    try:
        for db in BOARDS.glob("*/kanban.db"):
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                for (wp,) in c.execute(
                    "SELECT workspace_path FROM tasks WHERE status NOT IN ('done','archived')"
                ):
                    if wp:
                        live.add(str(wp).rstrip("/"))
                c.close()
            except Exception:
                continue
    except Exception:
        pass
    orphans = []
    now = time.time()
    for d in wt_root.iterdir():
        if not d.is_dir():
            continue
        if str(d).rstrip("/") in live:
            continue
        try:
            age_h = (now - d.stat().st_mtime) / 3600
        except OSError:
            continue
        if age_h < 24:  # give fresh worktrees a day
            continue
        try:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except Exception:
            size = 0
        orphans.append((d.name, size))
    if not orphans:
        print(f"[{datetime.now(timezone.utc).isoformat()}] orphan-worktree GC: none")
        return 0
    orphans.sort(key=lambda x: -x[1])
    total_gb = sum(s for _, s in orphans) / 1e9
    listing = "; ".join(f"{n} ({s/1e6:.0f}M)" for n, s in orphans[:10])
    msg = f"{len(orphans)} orphaned worktrees (~{total_gb:.1f}G): {listing}"
    if not dry_run:
        _notify("info", "task-lifecycle: orphaned worktrees", msg)
    print(f"[{datetime.now(timezone.utc).isoformat()}] orphan-worktree GC: {msg}")
    return 0


def enforce_plebeian_urgency(dry_run=False):
    """Stamp urgency=now on every non-terminal plebeian review/fix task and
    release any the urgency gate parked (status='scheduled'). Zero tokens;
    per-board fail-open so one bad DB never blocks the rest."""
    total = 0
    stamped: list[str] = []
    for board in PLEBEIAN_URGENCY_BOARDS:
        db = BOARDS / board / "kanban.db"
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(str(db), timeout=10)
            rows = conn.execute(
                "SELECT id, status, urgency FROM tasks "
                "WHERE status NOT IN ('done','archived','completed','cancelled')"
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"[urgency-now] {board}: read failed (fail-open): {e}", file=sys.stderr)
            continue
        n = 0
        for tid, st, urg in rows:
            needs = (urg != "now") or (st == "scheduled")
            if not needs:
                continue
            if dry_run:
                n += 1
                continue
            if urg != "now":
                subprocess.run(
                    [HERMES_PY, URGENCY_GATE, "set", board, tid, "now"],
                    capture_output=True, text=True, timeout=60,
                )
            # Release parked tasks (the urgency gate schedules unclassified /
            # price-held ones). cmd_set unblocks scheduled->now, but do it here
            # too so a task already at 'now' is still released.
            if st == "scheduled":
                subprocess.run(
                    [HERMES_BIN, "kanban", "--board", board, "unblock", tid,
                     "--reason", "plebeian review/fix: urgency=now (auto)"],
                    capture_output=True, text=True, timeout=60,
                )
            stamped.append(f"{board}/{tid}")
            n += 1
        if n:
            print(f"[{datetime.now(timezone.utc).isoformat()}] "
                  f"urgency-now: {board}: {n} task(s) -> now")
        total += n
    if total:
        print(f"[{datetime.now(timezone.utc).isoformat()}] "
              f"urgency-now: stamped {total} plebeian task(s): {', '.join(stamped[:8])}")
    return total


def status():
    state = _load_state()
    print(f"task-lifecycle-governor  headroom_ok={_headroom_ok()}")
    print(f"  state keys: {len(state)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Task-lifecycle governor")
    ap.add_argument("command", nargs="?", default="scan", choices=("scan", "status", "preventive", "orphan-worktree", "urgency-now"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        if args.command == "scan":
            return scan(dry_run=args.dry_run)
        if args.command == "status":
            return status()
        if args.command == "preventive":
            return preventive_cleanup(dry_run=args.dry_run)
        if args.command == "orphan-worktree":
            return orphan_worktree_gc(dry_run=args.dry_run)
        if args.command == "urgency-now":
            return enforce_plebeian_urgency(dry_run=args.dry_run)
    except Exception as exc:
        print(f"task-lifecycle-governor: internal error (fail-open): {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
