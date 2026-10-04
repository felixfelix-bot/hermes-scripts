#!/usr/bin/env python3
"""fleet_loop_guard.py — auto-block kanban cards stuck in a spawn/reclaim loop.

A card whose worker keeps dying (``dispatcher guard: dead worker pid``) is
reclaimed and re-dispatched every tick, burning worker slots and provider
tokens forever: ``kanban.failure_limit`` never trips because guard-reclaims do
not increment ``consecutive_failures`` (2026-09-17: a PCB card looped 46x).

This guard counts *reclaimed/blocked* runs per ``(board, task)`` in a rolling
window and **blocks** a card once it reaches ``kanban.loop_block_after`` (default
2), with a reason the operator can act on. Idempotent: terminal cards are
skipped. Bounded per tick.

Config-as-code:
  * threshold  <- config.yaml ``kanban.loop_block_after`` (default 2)
  * window     <- ``--window`` / env ``KANBAN_LOOP_WINDOW_S`` (default 6h)

Usage:
  fleet_loop_guard.py [--apply] [--window 21600] [--threshold 2]
                      [--board B] [--max N]
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS_GLOB = str(HERMES / "kanban" / "boards" / "*" / "kanban.db")
BAD_OUTCOMES = {"reclaimed", "blocked"}
RATE_LIMIT_OUTCOME = "rate_limited"
DEFAULT_RL_BLOCK_AFTER = 6     # >= this many rate_limited probes = lane outage
# Only park a rate-limit storm while it is ACTIVE: the newest probe must be
# this recent. A recovered lane stops probing, so stale runs cannot block it.
RL_RECENCY_S = 45 * 60
# Worker-death outcomes. The dispatcher's dead-pid reclaim writes
# ``outcome='crashed'`` (NOT 'reclaimed'), so counting only BAD_OUTCOMES made the
# guard blind to exactly the loops that burn worker slots (2026-09-22: a card
# crashed 4-6x/h and the guard reported "no looping cards"). Separate family +
# threshold + recency, mirroring the rate-limit-storm design.
CRASH_OUTCOMES = {"crashed", "stale", "timed_out", "spawn_failed"}
DEFAULT_CRASH_BLOCK_AFTER = 4   # >= this many worker crashes = loop / hardware
CRASH_RECENCY_S = 45 * 60       # only park an ACTIVE crash streak


def _config_int(key: str, env_name: str, default: int) -> int:
    env = os.environ.get(env_name, "").strip()
    if env.isdigit():
        return int(env)
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load((HERMES / "config.yaml").read_text()) or {}
        v = (cfg.get("kanban") or {}).get(key, default)
        return int(v) if str(v).isdigit() else default
    except Exception:
        return default


def config_threshold() -> int:
    """Read kanban.loop_block_after from the base config (fallback 2)."""
    return _config_int("loop_block_after", "KANBAN_LOOP_BLOCK_AFTER", 2)


def config_rl_threshold() -> int:
    """Read kanban.rate_limit_block_after (fallback 6).

    A card that only ever bounces off a provider quota wall is requeued by the
    dispatcher with a cooldown and NO failure counter, so it never produces a
    reclaimed/blocked run and ``loop_block_after`` never trips. Counting
    ``rate_limited`` runs separately (with a higher threshold, since a couple of
    probes are normal) parks a genuine lane outage instead of probing forever.
    """
    return _config_int("rate_limit_block_after",
                       "KANBAN_RATE_LIMIT_BLOCK_AFTER", DEFAULT_RL_BLOCK_AFTER)


def config_crash_threshold() -> int:
    """Read kanban.crash_block_after (fallback 4).

    Repeated worker deaths (dead-pid reclaims, ``outcome='crashed'``) are a loop
    or a hardware/provider fault, not a card bug. A couple of transient crashes
    are normal; the threshold parks a genuine streak so it stops burning slots.
    """
    return _config_int("crash_block_after",
                       "KANBAN_CRASH_BLOCK_AFTER", DEFAULT_CRASH_BLOCK_AFTER)


def loop_counts(runs: list[tuple]) -> tuple[int, int, int]:
    """Return ``(bad_runs, rate_limited_runs, crash_runs)`` for run rows.

    ``crash_runs`` counts worker-death outcomes (``CRASH_OUTCOMES``) that the
    dispatcher's dead-pid reclaim writes — the 2026-09-22 blind spot.
    """
    bad = sum(1 for _ts, status, outcome in runs
              if (outcome in BAD_OUTCOMES) or (status == "reclaimed"))
    rl = sum(1 for _ts, status, outcome in runs
             if outcome == RATE_LIMIT_OUTCOME or status == RATE_LIMIT_OUTCOME)
    crash = sum(1 for _ts, status, outcome in runs
                if outcome in CRASH_OUTCOMES or status in CRASH_OUTCOMES)
    return bad, rl, crash


def _active_enough(runs: list[tuple], matcher, recency_s: int,
                   now: float | None) -> bool:
    """True if the newest run matching ``matcher`` is within ``recency_s`` of now.

    ``matcher`` is an outcome string or a set of outcomes; ``status`` is matched
    too. Zero recency (or unknown ``now``) means "no recency requirement".
    """
    if not recency_s or now is None:
        return True
    match = {matcher} if isinstance(matcher, str) else set(matcher)
    newest = max((ts for ts, status, outcome in runs
                  if outcome in match or status in match), default=None)
    return newest is not None and (now - newest) <= recency_s


def is_looping(runs: list[tuple], threshold: int,
               rl_threshold: int | None = None, now: float | None = None,
               rl_recency_s: int = 0,
               crash_threshold: int | None = None,
               crash_recency_s: int = 0) -> bool:
    """True if this task is stuck in a spawn loop.

    ``runs`` is a list of ``(started_at, status, outcome)`` rows. Looping when
    reclaimed/blocked runs reach ``threshold``; or, when ``rl_threshold`` is
    set, rate_limited probes reach it; or, when ``crash_threshold`` is set,
    worker-death runs reach it. The rl/crash families additionally require the
    newest matching run to be within their recency window, so a recovered lane
    or a healed worker is not parked on a stale streak (an ACTIVE storm keeps
    producing fresh runs). Pure so it is unit-testable without a DB.
    """
    bad, rl, crash = loop_counts(runs)
    if bad >= threshold:
        return True
    if (rl_threshold is not None and rl >= rl_threshold
            and _active_enough(runs, RATE_LIMIT_OUTCOME, rl_recency_s, now)):
        return True
    if (crash_threshold is not None and crash >= crash_threshold
            and _active_enough(runs, CRASH_OUTCOMES, crash_recency_s, now)):
        return True
    return False


def board_loopers(db: str, since: float, threshold: int,
                  rl_threshold: int | None = None,
                  crash_threshold: int | None = None,
                  ) -> list[tuple[str, int, int, int, str]]:
    """Return ``(task_id, bad, rl, crash, status)`` for loopers."""
    out: list[tuple[str, int, int, int, str]] = []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except Exception:
        return out
    try:
        rows = c.execute(
            "SELECT task_id, started_at, status, outcome FROM task_runs "
            "WHERE started_at >= ? ORDER BY task_id, id DESC",
            (since,),
        ).fetchall()
    except Exception:
        c.close()
        return out
    by_task: dict[str, list[tuple]] = {}
    for tid, ts, status, outcome in rows:
        by_task.setdefault(tid, []).append((ts, status, outcome))
    # Only cards that are still actionable (ready/triage) are worth blocking.
    actionable = {}
    try:
        for tid, status in c.execute(
            "SELECT id, status FROM tasks WHERE status IN ('ready','triage')"
        ).fetchall():
            actionable[tid] = status
    except Exception:
        pass
    c.close()
    for tid, runs in by_task.items():
        if tid not in actionable:
            continue
        if is_looping(runs, threshold, rl_threshold, now=time.time(),
                      rl_recency_s=RL_RECENCY_S,
                      crash_threshold=crash_threshold,
                      crash_recency_s=CRASH_RECENCY_S):
            bad, rl, crash = loop_counts(runs)
            out.append((tid, bad, rl, crash, actionable[tid]))
    return out


def needs_archive(status: str) -> bool:
    """True when a looped card must be archived rather than blocked.

    ``kanban block`` only accepts ``running``/``ready`` and ``promote`` only
    accepts ``todo``/``blocked`` — a looped card that landed in ``triage``
    (the same-kind re-block guard routes it there) can be neither blocked nor
    promoted ("cannot block/promote <id>"). That is the 2026-09-20 bug this
    guard hit (`FAILED to block router-maintenance/t_d359cb7c`). Archiving is
    the only supported terminal transition from ``triage``; the row is kept
    (recoverable) and the spawn/reclaim loop stops.
    """
    return status == "triage"


def block_task(board: str, tid: str, reason: str, hermes_bin: str,
               status: str = "") -> bool:
    base = [hermes_bin, "kanban", "--board", board]
    try:
        if needs_archive(status):
            subprocess.run(base + ["comment", tid, reason],
                           capture_output=True, text=True, timeout=30)
            r = subprocess.run(base + ["archive", tid],
                               capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        r = subprocess.run(
            base + ["block", tid, reason],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--window", type=int,
                    default=int(os.environ.get("KANBAN_LOOP_WINDOW_S", "21600")))
    ap.add_argument("--threshold", type=int, default=None)
    ap.add_argument("--rl-threshold", type=int, default=None,
                    help="rate_limited probes before a lane-outage card is parked")
    ap.add_argument("--crash-threshold", type=int, default=None,
                    help="worker-death runs before a crash-loop card is parked")
    ap.add_argument("--board", default="")
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--hermes-bin", default=os.path.join(
        str(HERMES), "hermes-agent", "venv", "bin", "hermes"))
    args = ap.parse_args()

    threshold = args.threshold if args.threshold is not None else config_threshold()
    rl_threshold = (args.rl_threshold if args.rl_threshold is not None
                    else config_rl_threshold())
    crash_threshold = (args.crash_threshold if args.crash_threshold is not None
                       else config_crash_threshold())
    since = time.time() - args.window

    dbs = ([str(HERMES / "kanban" / "boards" / args.board / "kanban.db")]
           if args.board else sorted(glob.glob(BOARDS_GLOB)))

    blocked = 0
    for db in dbs:
        board = Path(db).parent.name
        if not os.path.exists(db):
            continue
        for tid, bad, rl, crash, status in board_loopers(
                db, since, threshold, rl_threshold, crash_threshold):
            if blocked >= args.max:
                print(f"loop-guard: hit --max {args.max}, stopping")
                return 0
            if bad >= threshold:
                detail = (f"looped {bad}x (dispatcher guard: dead worker pid)")
                knob = f"kanban.loop_block_after={threshold}"
            elif crash >= crash_threshold:
                detail = (f"{crash}x crashed runs (worker died — provider / "
                          f"hardware outage, not a card bug)")
                knob = f"kanban.crash_block_after={crash_threshold}"
            else:
                detail = (f"{rl}x rate_limited probes (provider lane outage / "
                          f"quota wall — not a card bug)")
                knob = f"kanban.rate_limit_block_after={rl_threshold}"
            reason = (f"auto: {detail} in {args.window // 3600}h — blocked by "
                      f"fleet_loop_guard ({knob})")
            if args.apply:
                if block_task(board, tid, reason, args.hermes_bin, status):
                    verb = "archived" if needs_archive(status) else "blocked"
                    print(f"loop-guard: {verb} {board}/{tid} "
                          f"(bad={bad} rate_limited={rl} crash={crash})")
                    blocked += 1
                else:
                    print(f"loop-guard: FAILED to block {board}/{tid}")
            else:
                print(f"[dry] would block {board}/{tid} "
                      f"(bad={bad} rate_limited={rl} crash={crash})")
                blocked += 1
    if blocked == 0:
        print("loop-guard: no looping cards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
