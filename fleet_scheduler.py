#!/usr/bin/env python3
"""fleet_scheduler.py — pull-based fleet work loop (K.5).

Each node runs this. Every tick it:
  1. ADVERTISES its own ready kanban tasks that fit the pool (full pull).
  2. INGESTS fleet-task/claim/done events from buzz.
  3. ROUTES each unclaimed task via fleet_queue.route() (fit + headroom gate).
  4. CLAIMS what it should (publishes fleet-claim; deterministic lease).
  5. EXECUTES won tasks locally through the admission-gated crash wrapper.
  6. Publishes fleet-done when a local offload finishes.

Long-lived (systemd Type=simple). Concurrency is bounded by FLEET_OFFLOAD_MAX
and the crash wrapper's admission cap.

Usage: fleet_scheduler.py [--once] [--max N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_queue as fq  # type: ignore
import fleet_ownership as fo  # type: ignore
try:  # Phase V6.0c — per-run disk footprint (fail-soft)
    import disk_meter as dm  # type: ignore
except Exception:  # noqa: BLE001
    dm = None  # type: ignore

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
WRAPPER = HERMES / "scripts" / "kanban-crash-wrapper.sh"
STATE = BOT / "fleet_queue_state.json"

# Appended to every offload worker prompt. The worker's turn budget is a hard
# cap; a cap-out must leave a pushed, resumable savepoint, not a dirty tree.
# Mirrors state/worker-base/SOUL.md "Death-Proof Every Task".
DEATHPROOF = (
    "Death-proof the work: commit AND push after each verified milestone "
    "(never leave more than ~15 tool-calls uncommitted); keep PROGRESS.md at the "
    "worktree root (finding -> status -> files touched); write your FULL report "
    "to REPORT.md BEFORE your final reply; if you run out of budget, state "
    "exactly which step you stopped at and list the REMAINING steps as a "
    "numbered list, and never claim a commit/push/test/PR you did not observe. "
    "A short final reply pointing at the on-disk report is a success."
)
# Durable "already wrote back" ledger. The in-state `written_back` flag on an
# advertised entry resets whenever the state file is purged/rebuilt (e.g. the
# 2026-09-17 unfit-repo purge), which let replayed stale fleet-done events close
# reopened cards. A ledger on disk survives state purges.
WRITEBACK_LEDGER = BOT / "fleet_writeback_ledger.json"
FIT = BOT / "fleet_fit.json"
BOARDS = BOT / "fleet_boards.json"      # optional list of boards to advertise
FLEET_MAP = BOT / "fleet_map"           # D-126 public/private classification
QUARANTINES = (HERMES / "ESTOP", BOT / ".dispatch_frozen", BOT / ".fleet_quarantine",
               BOT / ".fleet_offload_disabled")
_RUN_TTL = int(os.environ.get("FLEET_RUN_TTL", "3600"))
_MAX_STATE_TASKS = int(os.environ.get("FLEET_MAX_STATE_TASKS", "400"))
# A local card held for a peer is only valid while that peer heartbeats. If no
# fleet-running heartbeat arrives within _HOLD_TTL, the hold is released so the
# work can't sit blocked forever with no live owner (AV-FIX-1 freeze).
_HOLD_TTL = int(os.environ.get("FLEET_HOLD_TTL", "1800"))
_HEARTBEAT_S = int(os.environ.get("FLEET_HEARTBEAT_S", "120"))
_running: dict[str, subprocess.Popen] = {}
_last_beat: dict[str, float] = {}
_run_start: dict[str, float] = {}
_disk_before: dict[str, int] = {}   # Phase V6.0c: free bytes at run start
_run_profile: dict[str, str] = {}


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write(p, v):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


def _stale_dispatch_frozen() -> bool:
    """True if `.dispatch_frozen` exists but is stale per freeze_policy.json.

    Defense-in-depth (2026-10-08): four nodes sat `.dispatch_frozen` for days
    because their freeze-guard timer was disabled, so `_frozen()` blocked all
    dispatch and the scheduler kept yielding work to nodes that never ran it.
    A freeze older than the policy ttl (with stale_clear on) must not block.
    """
    p = BOT / ".dispatch_frozen"
    if not p.exists():
        return False
    pol = _read(BOT / "freeze_policy.json", {}) or {}
    if not pol.get("stale_clear", True):
        return False
    try:
        ttl = int(pol.get("ttl_s", 1800))
    except (TypeError, ValueError):
        ttl = 1800
    if ttl <= 0:
        return False  # sticky freeze
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return False
    return age > ttl


def _frozen() -> bool:
    # Hard markers, never auto-lifted: an operator ESTOP, an L6 quarantine, or
    # an explicit offload disable.
    if (HERMES / "ESTOP").exists() or (BOT / ".fleet_quarantine").exists():
        return True
    if (BOT / ".fleet_offload_disabled").exists():
        return True
    df = BOT / ".dispatch_frozen"
    if df.exists():
        if _stale_dispatch_frozen():
            # Self-heal: the freeze-guard timer normally clears this; if it is
            # disabled/absent, clear the stale marker here so dispatch resumes.
            try:
                df.unlink()
                print("[fleet-sched] cleared stale .dispatch_frozen (age > ttl)",
                      flush=True)
            except OSError:
                pass
        else:
            return True
    return False


def _hermes() -> str:
    for c in (HERMES / "hermes-agent/venv/bin/hermes",
              Path.home() / ".local/bin/hermes"):
        if c.exists():
            return str(c)
    return "hermes"


def _boards_cfg() -> dict:
    b = _read(BOARDS, None)
    if isinstance(b, list):
        return {"boards": b, "exclude": [], "hold": False}
    if isinstance(b, dict):
        return {"boards": b.get("boards", []), "exclude": b.get("exclude", []),
                "hold": bool(b.get("hold", False))}
    return None


def _boards() -> list[str]:
    cfg = _boards_cfg()
    if cfg is not None and isinstance(cfg.get("boards"), list) and cfg["boards"]:
        return [b for b in cfg["boards"] if b not in cfg.get("exclude", [])]
    # "all" (or no config): every board with a DB, minus excludes.
    exclude = set((cfg or {}).get("exclude", []))
    out = [d.parent.name for d in sorted((HERMES / "kanban" / "boards").glob("*/kanban.db"))]
    return [b for b in out if b not in exclude]


def _hold_enabled() -> bool:
    return bool((_boards_cfg() or {}).get("hold", False))


def _public_boards() -> dict | None:
    """D-126 public classification; None only if the map is missing."""
    return _load_map("public_boards.json")


def _local_boards() -> dict | None:
    """Phase W2 — explicit node-local override list (never advertised)."""
    return _load_map("local_only_boards.json")


def _boardset(m) -> set:
    """Normalize a map to a set of board slugs. Accepts either
    {"boards":[...]} (operator allowlist / local-only) or {slug:{...}} (classifier)."""
    if not m:
        return set()
    if isinstance(m, dict) and isinstance(m.get("boards"), list):
        return {str(b) for b in m["boards"]}
    if isinstance(m, list):
        return {str(b) for b in m}
    if isinstance(m, dict):
        return set(m.keys())
    return set()


def _load_map(name: str) -> dict | None:
    p = FLEET_MAP / name
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, (dict, list)) else None
    except Exception:
        return None


def _advertise_transport(board: str, public: dict | None,
                         private: dict | None,
                         local: dict | None = None) -> str | None:
    """Phase W2 semantics: the board is advertised to the PUBLIC bus only if it
    is in the operator public allowlist, and to the PRIVATE fleet ledger only if
    it is explicitly opted in (`private_offload_boards.json`). Everything else is
    NODE-LOCAL. Fail closed only when no map exists at all."""
    if public is None and private is None and local is None:
        return None
    if board in _boardset(local):
        return None  # explicit node-local
    if board in _boardset(public):
        return "public"
    if board in _boardset(private):
        return "private"
    return None  # default: node-local


def _kanban(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run([_hermes(), "kanban"] + args, capture_output=True,
                           text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _repo_for_board(board: str, boards_root: "Path | None" = None) -> str:
    """Resolve the repo directory a board's tasks require (offload fit).

    A board is the isolation boundary, but not every board is itself a repo
    (e.g. review lanes like ``plebeian-pr-reviews``). Advertising
    ``repo:<slug>`` for such a board tags its tasks with a requirement no
    node's fit profile can satisfy, so every node skips them as
    ``unfit: missing repo:<slug>`` and the card strands in ``ready`` forever.

    Resolution order:
      1. ``board.json`` -> ``repo``            (explicit; operator-declared)
      2. ``board.json`` -> ``local_only``      -> no repo requirement ("")
      3. ``board.json`` -> ``default_workdir`` basename (``repos/<name>``)
      4. fall back to the slug                 (repo-named boards unchanged)
    ``fleet`` is a coordination board and never requires a repo.
    """
    if board == "fleet":
        return ""
    root = Path(boards_root) if boards_root else (HERMES / "kanban" / "boards")
    cfg = _read(root / board / "board.json", {}) or {}
    repo = str(cfg.get("repo") or "").strip()
    if repo:
        return repo
    if cfg.get("local_only"):
        return ""
    wd = str(cfg.get("default_workdir") or "").strip().rstrip("/")
    if wd:
        name = os.path.basename(wd)
        if name:
            return name
    return board


def advertise_local(state: dict) -> int:
    """Publish fleet-task events for this node's ready tasks not yet advertised."""
    advertised = state.setdefault("advertised", {})
    boards = _boards()
    if not boards:
        return 0
    public = _public_boards()
    private = _load_map("private_offload_boards.json")
    local = _local_boards()
    if public is None and private is None and local is None:
        print("[fleet-sched] no visibility map — advertising nothing (fail-closed)")
        return 0
    me = fq._self()
    import sqlite3
    n = 0
    budget = int(os.environ.get("FLEET_ADVERTISE_PER_TICK", "20"))
    for board in boards:
        if n >= budget:
            break
        transport = _advertise_transport(board, public, private, local)
        if transport is None:
            continue  # node-local / unclassified — never advertised
        db = HERMES / "kanban" / "boards" / board / "kanban.db"
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            statuses = "'ready'"
            if os.environ.get("FLEET_ADVERTISE_REVIEWS", "").lower() in ("1", "true", "yes"):
                statuses = "'ready','review'"
            rows = c.execute(
                f"SELECT id,title,body,assignee FROM tasks WHERE status IN ({statuses}) "
                "LIMIT 50").fetchall()
            c.close()
        except Exception:
            continue
        for tid, title, body, assignee in rows:
            key = f"{board}:{tid}"
            if advertised.get(key):
                continue
            repo = _repo_for_board(board)
            task = fq.classify(board, repo, title or "", body or "")
            payload = {"type": "fleet-task", "id": key, "board": board,
                       "repo": repo, "title": title or tid,
                       "body": (body or "")[:4000],
                       "resource_class": task["resource_class"],
                       "requires": task["requires"], "exclusions": task["exclusions"],
                       "tags": task["tags"], "actor": me, "created_ts": time.time(),
                       "source_task": tid, "assignee": assignee,
                       "private": transport == "private"}
            if fq.publish_any(payload, "fleet-task", transport == "private"):
                advertised[key] = {"ts": int(time.time()), "board": board,
                                   "task": tid, "held": None, "transport": transport,
                                   "assignee": assignee, "tags": task.get("tags") or []}
                n += 1
    return n


def _caps() -> dict:
    """Per-dimension resource policy (mirrors fleet_arbiter.DEFAULTS)."""
    caps = {"max_load_per_cpu": 0.8, "min_mem_available_mb": 1536,
            "load_storm_per_cpu": 12.0, "max_workers": 4}
    cfg = _read(BOT / "fleet.json", {}) or {}
    caps.update(cfg.get("caps", {}) or {})
    return caps


def _peer_health_map(peers: "list[dict] | None") -> dict:
    return {p["node"]: p for p in (peers or [])
            if isinstance(p, dict) and p.get("node")}


def _winner_holdable(winner: str, key: str, state: dict,
                     peers: "list[dict] | None", health: dict,
                     caps: dict) -> "tuple[bool, str]":
    """t_dd8ff7ca: only hold a local card for a LIVE, HEALTHY, FIT winner.

    A single saturated or unfit node must never brake the whole fleet. If the
    winner is not demonstrably able to take the work, the card stays local
    instead of being blocked (blocking propagates to every peer via kanban
    sync, which is how one bad node denied work fleet-wide).
    """
    me = fq._self()
    if winner == me:
        return True, ""            # self is trivially live/healthy/fit
    h = _peer_health_map(peers).get(winner)
    if not h:
        return False, "no-fresh-health"
    load = float(h.get("load1_per_cpu", 0) or 0)
    if load >= float(caps.get("load_storm_per_cpu", 12.0)):
        return False, f"winner load/cpu {load} (storm)"
    mem = float(h.get("mem_available_mb", 1e9) or 1e9)
    if mem < float(caps.get("min_mem_available_mb", 1536)):
        return False, f"winner mem {int(mem)}MB"
    task = (state.get("tasks", {}) or {}).get(key) or {}
    ok, why = fq.fit_ok(h.get("fit") or {}, task)
    if not ok:
        return False, f"winner unfit: {why}"
    return True, ""


def _hold_ttl() -> int:
    cfg = _balance_cfg()
    try:
        return int(cfg.get("hold_ttl_s", os.environ.get("FLEET_HOLD_TTL", str(_HOLD_TTL))))
    except (TypeError, ValueError):
        return _HOLD_TTL


def _winner_is_live(state: dict, key: str, task_id: str, winner: str,
                    now: float) -> bool:
    """True when `winner` has a fresh fleet-running heartbeat for this task."""
    runs = state.get("running", {})
    r = runs.get(key) or runs.get(task_id) or {}
    return (r.get("node") == winner
            and now - float(r.get("ts", 0)) <= _hold_ttl())


def _reviewer_assignee(name: str) -> bool:
    n = str(name or "").lower()
    return n.startswith("worker-reviewer") or n.startswith("reviewer")


def _is_review_task(info: dict) -> bool:
    """True for review-lane work, which must NEVER be offload-held.

    Review cards are small, local and time-sensitive; holding one for a peer is
    how t_0f5112de was parked and then silently swept to `done` with no published
    review. Detect by board name, assignee profile, reviewer tag, or (for legacy
    advertised entries without an assignee) a board-DB lookup.
    """
    if "review" in str(info.get("board") or "").lower():
        return True
    if _reviewer_assignee(info.get("assignee")):
        return True
    for t in (info.get("tags") or []):
        ts = str(t).lower()
        if ts.startswith("reviewer:") or ts.startswith("review:"):
            return True
    if not info.get("assignee") and info.get("task") and info.get("board"):
        try:
            import sqlite3
            db = HERMES / "kanban" / "boards" / info["board"] / "kanban.db"
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = c.execute("select assignee from tasks where id=?",
                                (info["task"],)).fetchone()
            finally:
                c.close()
            if row and _reviewer_assignee(row[0]):
                return True
        except Exception:
            pass
    return False


_TERMINAL_STATUSES = ("done", "completed", "archived", "gave_up", "crashed",
                      "timed_out")
# `kanban block` accepts ready/running; `kanban unblock` accepts blocked/scheduled.
_HOLDABLE_STATUSES = ("ready", "running")
_UNBLOCKABLE_STATUSES = ("blocked", "scheduled")


def _card_status(board: str, tid: str) -> "str | None":
    """Read a board-DB card's status, or None when unknown/unreadable."""
    if not board or not tid:
        return None
    try:
        import sqlite3
        db = HERMES / "kanban" / "boards" / board / "kanban.db"
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = c.execute("select status from tasks where id=?",
                            (tid,)).fetchone()
        finally:
            c.close()
    except Exception:
        return None
    return str(row[0]) if row else None


def _card_terminal(board: str, tid: str) -> bool:
    return _card_status(board, tid) in _TERMINAL_STATUSES


def _card_holdable(board: str, tid: str) -> bool:
    """Only ready/running cards can be blocked, so only those are holdable."""
    return _card_status(board, tid) in _HOLDABLE_STATUSES


_RE_BLOCK_BODY = re.compile(r"^BLOCKED: fleet-offload:([A-Za-z0-9._-]+)$")


def _fleet_block_ts(board: str, tid: str, now: float,
                    ttl: float) -> "tuple[str, float] | None":
    """Return ``(winner, ts)`` when `tid` still carries OUR recent offload hold.

    The kernel stores a block's reason only as a ``BLOCKED: fleet-offload:<node>``
    comment (the tasks table has no block_reason column), so the card's comment
    trail is the authoritative record. Only the NEWEST fleet-offload comment is
    considered, and only while it is younger than `ttl`: that keeps a recently
    re-advertised card from being blocked (and commented) a second time while
    never re-adopting the September backlog of dead holds.
    """
    if not board or not tid:
        return None
    try:
        import sqlite3
        db = HERMES / "kanban" / "boards" / board / "kanban.db"
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = c.execute(
                "select body, created_at from task_comments where task_id=? and "
                "body like 'BLOCKED: fleet-offload:%' order by id desc limit 1",
                (tid,)).fetchone()
        finally:
            c.close()
    except Exception:  # noqa: BLE001 — unreadable DB: caller falls back to blocking
        return None
    if not row:
        return None
    m = _RE_BLOCK_BODY.match(str(row[0] or "").strip())
    if not m:
        return None
    ts = float(row[1] or 0)
    if ttl > 0 and now - ts > ttl:
        return None
    return m.group(1), ts


def _db_in_flight(board: str, tid: str, now: float) -> bool:
    """True when this node's board DB shows a live run for `tid`.

    Read-only: `status='running'`, a non-NULL `current_run_id` (set at claim
    time, i.e. before the worker is spawned), or a `worker_pid` that is still
    alive. A genuinely queued card (ready, no run pointer, no live pid) is
    still holdable — that is the whole point of the hold.
    """
    if not board or not tid:
        return False
    import sqlite3
    dbs = (HERMES / "kanban" / "boards" / board / "kanban.db",
           HERMES / "kanban" / "kanban.db")  # board DB, then the default board
    for db in dbs:
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = c.execute(
                    "select status, current_run_id, worker_pid from tasks"
                    " where id=?", (tid,)).fetchone()
            finally:
                c.close()
        except Exception:  # noqa: BLE001 — unreadable DB: fall through (no claim)
            continue
        if not row:
            return False
        status, run_id, pid = row
        if status == "running" or run_id is not None:
            return True
        if pid and fo._pid_alive(pid):
            return True
        return False
    return False


def _has_local_run(info: dict, now: float) -> bool:
    """True when a LOCAL worker is already executing this advertised card.

    Holding a card that is in flight is what dead-ended t_7027694a (board
    plebeian-my-prs, 2026-09-19): `block` NULLs `current_run_id`, after which the
    live session's run-id-guarded `complete`/`block`/`review` all refuse, so the
    worker can only exit by skipping its terminal board call (t_81cb7a13).
    Holds are for QUEUED cards; running work is never touched.
    """
    tid = str(info.get("task") or "")
    if not tid:
        return False
    if tid in _running:                       # started by this scheduler
        return True
    if fo.live_lock(tid, ttl=_RUN_TTL):       # offload lock with a live pid
        return True
    return _db_in_flight(str(info.get("board") or ""), tid, now)


def apply_holds(state: dict, winners: dict, now: float | None = None, *,
                peers: "list[dict] | None" = None,
                health: dict | None = None) -> int:
    """When hold is enabled, block the local copy of any advertised task that a
    winner (self or peer) is now responsible for, so the LOCAL dispatcher does
    not also run it. Reversible; local wins are set to review on reap.

    D-131: a hold whose winner never *started* is reclaimed after `_HOLD_TTL`
    (see release_stale_holds), so work can never strand when the peer goes away.
    A winner previously released for failing to ack stays denied until it
    publishes a fresh heartbeat — otherwise the very next tick would re-block
    the card we just freed.

    t_dd8ff7ca: holds are **per-node and winner-aware**. A card is only held
    for a winner that is demonstrably live, healthy and fit. Otherwise it stays
    local — a single saturated node must never brake the fleet (a held card's
    block propagates to every peer via kanban sync).
    """
    if not _hold_enabled():
        return 0
    now = now if now is not None else time.time()
    denied = state.setdefault("hold_denied", {})
    skipped = state.setdefault("hold_skipped",
                               {"unhealthy": 0, "unfit": 0, "no-health": 0})
    skipped["unhealthy"] = skipped["unfit"] = skipped["no-health"] = 0
    caps = _caps()
    n = 0
    for key, info in state.get("advertised", {}).items():
        if not isinstance(info, dict):
            continue
        winner = winners.get(key)
        if not winner or info.get("held"):
            continue
        if peers is not None:
            holdable, why = _winner_holdable(winner, key, state, peers,
                                             health or {}, caps)
            if not holdable:
                if "unfit" in why:
                    skipped["unfit"] += 1
                elif "health" in why:
                    skipped["no-health"] += 1
                else:
                    skipped["unhealthy"] += 1
                continue
        tid = info.get("task")
        # A terminal OR already-non-blockable card must not be held: `block`
        # would be rejected and `unblock` could never release it. Only skip
        # when the status is positively known; an unreadable DB keeps the old
        # fail-safe behavior (attempt the hold).
        _st = _card_status(info.get("board"), tid)
        if _st is not None and _st not in _HOLDABLE_STATUSES:
            info["held"] = None
            info.pop("held_ts", None)
            info.pop("held_at", None)
            continue
        # Completion integrity: never offload-hold review work (it is local and
        # must publish; holding parks it and risks a silent done).
        if _is_review_task(info):
            continue
        # Holds are for QUEUED cards only. Blocking a card whose local worker is
        # already in flight NULLs current_run_id, and that live session's
        # run-id-guarded complete/block/review then all refuse (t_81cb7a13).
        if _has_local_run(info, now):
            continue
        # Dedupe (t_00fa8726): the card is holdable again but may ALREADY carry
        # our `fleet-offload` hold from an earlier tick — the state entry lost
        # `held` (purge, _prune_state eviction, scheduler restart) or another
        # writer promoted/re-opened the card. Blocking again appends a second
        # `BLOCKED: fleet-offload:<winner>` comment, and the kernel counts
        # repeats (`block_loop_detected`) and then ARCHIVES the card, which is
        # how t_a8fcf378 (recurrences=2) and t_67f3b03e (recurrences=3) died.
        # Adopt the live block instead of rewriting it.
        existing = _fleet_block_ts(info.get("board"), tid, now, _hold_ttl())
        if existing and existing[0] == winner:
            info["held"] = winner
            info["held_ts"] = existing[1]
            continue
        denied_for = denied.get(tid) or {}
        had_deny = bool(denied_for.get(winner))
        if had_deny:
            # A hold for this winner was already released once because it never
            # acked. Re-block ONLY when the winner publishes a fresh heartbeat;
            # otherwise a permanently-dead peer gets re-held every TTL forever
            # (2026-09-20: 58 stale dq05 holds churned through hermes-kanban
            # subprocesses). Keep it denied while the peer stays dark.
            if not _winner_is_live(state, key, tid, winner, now):
                denied_for[winner] = now + _hold_ttl()
                continue
            denied_for.pop(winner, None)  # peer is alive again — allow the hold
        rc, out = _kanban(["--board", info["board"], "block", tid,
                           f"fleet-offload:{winner}"])
        if rc == 0:
            info["held"] = winner
            info["held_ts"] = now
            n += 1
    if any(skipped.values()):
        print(f"[fleet-sched] hold skipped (winner not holdable): {skipped}")
    return n


def release_stale_holds(state: dict, now: float | None = None) -> int:
    """Unblock local cards held for a winner that never acked within _HOLD_TTL.

    The winner is denied until it publishes a fresh fleet-running heartbeat, so
    the next tick cannot immediately re-block the card we just freed.
    """
    if not _hold_enabled():
        return 0
    now = now if now is not None else time.time()
    ttl = _hold_ttl()
    denied = state.setdefault("hold_denied", {})
    n = 0
    for key, info in state.get("advertised", {}).items():
        if not isinstance(info, dict):
            continue
        winner = info.get("held")
        if not winner:
            continue
        # accept held_ts (this impl) or held_at (older deployed state)
        held_ts = float(info.get("held_ts") or info.get("held_at") or 0)
        if now - held_ts <= ttl:
            continue
        tid = info.get("task")
        # Nothing to undo unless the card is currently blocked/scheduled
        # (`unblock` fails otherwise) — drop the stale hold instead of retrying
        # it every tick. Only when the status is KNOWN; an unreadable DB keeps
        # the old behavior (attempt the unblock). (2026-09-20: 58 stale holds
        # on todo/triage cards churned `hermes kanban unblock` forever.)
        _st = _card_status(info.get("board"), tid)
        if _st is not None and _st not in _UNBLOCKABLE_STATUSES:
            # Nothing to unblock: another writer already released/promoted the
            # card. Deny the winner anyway — dropping the hold WITHOUT the
            # guard is the t_00fa8726 root cause: apply_holds() then re-blocks
            # on the very next tick and the kernel appends a SECOND
            # `BLOCKED: fleet-offload:<winner>` comment, which is what tripped
            # block_loop_detected on t_a8fcf378 (gap 2.9h) and t_67f3b03e.
            denied.setdefault(tid, {})[winner] = now + ttl
            info["held"] = None
            info.pop("held_ts", None)
            info.pop("held_at", None)
            continue
        if _winner_is_live(state, key, tid, winner, now):
            continue
        rc, _out = _kanban(["--board", info["board"], "unblock", tid])
        if rc == 0:
            info["held"] = None
            info.pop("held_ts", None)
            info.pop("held_at", None)
            denied.setdefault(tid, {})[winner] = now + ttl
            n += 1
            print(f"[fleet-sched] released stale offload hold {key} "
                  f"(winner {winner} never acked within {ttl}s)")
    return n


def heartbeat(state: dict, now: float | None = None) -> int:
    """Publish fleet-running for in-flight tasks at most every _HEARTBEAT_S so a
    holding peer can tell "slow to start" from "never started"."""
    now = now if now is not None else time.time()
    me = fq._self()
    n = 0
    for tid in list(_running):
        if now - float(_last_beat.get(tid, 0) or 0) < _HEARTBEAT_S:
            continue
        task = state.get("tasks", {}).get(tid, {})
        ev = {"type": "fleet-running", "id": tid, "actor": me, "ts": now}
        if fq.publish_any(ev, "fleet-running", bool(task.get("private"))):
            _last_beat[tid] = now
            n += 1
    return n


def _profile_for(task: dict) -> str:
    """D-130 hybrid cascade + D-131 reviewer routing.

    An explicit ``worker-*`` assignee WINS over the heuristics — a card routed
    to ``worker-reviewer-kimi`` must run on the Kimi reviewer profile (review
    board), not the generic heavy/coding lane. Otherwise: reviewer marker ->
    worker-reviewer-<family>; heavy/coding -> worker-heavy; light/docs -> the
    node's default worker profile.
    """
    assignee = str(task.get("assignee") or "").strip()
    if assignee.startswith("worker-"):
        return assignee
    rev = fq.reviewer_profile(task)
    if rev:
        return rev
    base = _read(FIT, {}).get("worker_profile", "worker-base")
    return "worker-heavy" if fq.is_heavy_or_coding(task) else base


def _already_claimed(state: dict, tid: str, me: str) -> bool:
    return any(e.get("node") == me
               for e in (state.get("claims", {}) or {}).get(tid, []))


def _started(state: dict, tid: str, node: str) -> bool:
    """True if `node` has marked the task started — running work is never taken."""
    return any(e.get("node") == node and e.get("started")
               for e in (state.get("claims", {}) or {}).get(tid, []))


def _sizing_note(task: dict, profile: str, log=print) -> None:
    """Advisory: log when a card looks over its assignee's turn cap.

    Never raises and never blocks dispatch — it only surfaces a likely cap-out
    so the manager can split/scope. See scripts/fleet/task_sizing.py.
    """
    try:
        import task_sizing as _ts  # noqa: PLC0415 (same dir, best-effort)
        cap = _ts.profile_cap(profile)
        a = _ts.assess(task.get("title", ""), task.get("body", ""), cap)
        if a["over"]:
            log(f"[fleet-sched] {task.get('id')} sizing: est ~{a['required']} "
                f"turns > cap {a['cap']} ({a['kind']}, files={a['est_files']}, "
                f"tests={a['est_tests']}) — consider splitting")
    except Exception:  # noqa: BLE001 (advisory only)
        pass


def _offload_prompt(task: dict, wd, repo: str) -> str:
    """Prompt for a fleet offload worker: task body + shipping discipline."""
    return (
        f"You are a fleet offload worker. Repo: {repo or task.get('board')}. "
        f"Workspace: {wd}. Task: {task.get('title')}. "
        f"{task.get('body', '')}\n\n"
        f"Do the work with tools, then ship it. {DEATHPROOF}"
    )


def _run_env(tid: str, me: str, run_id: str) -> dict:
    """Spawn env for an offload worker, with per-run session attribution.

    Without HERMES_SESSION_ID the worker's loopback model calls reach the
    router with no X-Hermes-Session header and land as unattributed burn
    (session_id NULL): invisible to per-task cost accounting and a trigger for
    attribution-burn-guard. Pin one id per (node, task, run) and override any
    value inherited from the scheduler's own environment.
    """
    env = dict(
        os.environ,
        HERMES_FLEET_CAP=str(_offload_max()),
        HERMES_SESSION_ID=f"fleet:{me}:{tid}:{run_id}",
    )
    # Do NOT export HERMES_KANBAN_TASK here. Setting it puts `hermes chat` into
    # kanban-worker mode, which requires the worker to post a terminal board op
    # (`complete`/`block`/`review`) and otherwise exits 78 (EX_CONFIG). But an
    # offload worker's job is to DO the work and ship it — THIS scheduler writes
    # the board back (see _write_back_local). Leaving the marker set made every
    # offloaded card end rc=78 and get written back `blocked` (the 2026-10-09
    # block-loop: "workers spawn, get killed on arrival").
    env.pop("HERMES_KANBAN_TASK", None)
    return env


def _worker_argv(profile: str, prompt: str) -> list[str]:
    """Argv for an offload worker.

    Prefer the crash wrapper (it captures diagnostics and enforces the admission
    gate), but fall back to the real hermes binary when it is absent. A node
    that never had the wrapper installed otherwise fails EVERY offloaded card
    with rc=127, which writes the card back to `blocked` — the 2026-10-09
    fleet-load incident. The scheduler's own `len(_running) < cap` still bounds
    concurrency when the wrapper is missing.
    """
    if WRAPPER.exists():
        return ["bash", str(WRAPPER), "-p", profile, "chat", "-q", prompt]
    return [_hermes(), "-p", profile, "chat", "-q", prompt]


def _active_offload_pids() -> set[int]:
    """PIDs of live offload workers on this node (from the per-task lock dir).

    The concurrency cap must count these, not just this process's `_running`:
    a scheduler restart empties `_running` but the workers keep running, so
    without this a restart re-spawns up to `cap` more and the node
    oversubscribes (2026-10-09: 19 workers on an 8-core x280).
    """
    pids: set[int] = set()
    try:
        for f in fo.LOCK_DIR.glob("*.json"):
            rec = _read(f, {}) or {}
            pid = rec.get("pid")
            if pid and fo._pid_alive(pid):
                pids.add(int(pid))
    except Exception:  # noqa: BLE001
        pass
    return pids


def execute(task: dict) -> None:
    tid = task["id"]
    profile = _profile_for(task)
    _sizing_note(task, profile)
    repo = task.get("repo") or ""
    wd = Path.home() / "repos" / repo if repo else HERMES
    me = fq._self()
    # Don't duplicate work across a scheduler restart: a live worker holds the lock.
    ll = fo.live_lock(tid, ttl=_RUN_TTL)
    if ll and ll.get("pid"):
        print(f"[fleet-sched] {tid} already running (pid {ll['pid']}) — skip")
        return
    run_id = f"{me}-{int(time.time())}"
    if not fo.acquire_lock(tid, me, run_id, ttl=_RUN_TTL):
        print(f"[fleet-sched] {tid} lock held elsewhere — skip")
        return
    fo.record(tid, me, "running", repo=repo, branch=task.get("branch", ""),
              run_id=run_id)
    prompt = _offload_prompt(task, wd, repo)
    cmd = _worker_argv(profile, prompt)
    env = _run_env(tid, me, run_id)
    cmd = _wrap_cgroup(cmd)
    try:
        p = subprocess.Popen(cmd, cwd=str(wd) if wd.exists() else str(HERMES),
                             env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        _running[tid] = p
        _run_start[tid] = time.time()
        if dm is not None:
            try:
                _disk_before[tid] = dm.free_used(str(HERMES))[0]
            except Exception:  # noqa: BLE001
                pass
        _run_profile[tid] = profile
        fo.set_lock_pid(tid, p.pid)
        # D-130: mark the lease started so peers never preempt running work.
        fq.publish_any(
            {"type": "fleet-claim", "id": tid, "actor": me,
             "claim_ts": time.time(),
             "headroom": fq.headroom(_read(BOT / "fleet_health.json", {})),
             "started": True},
            "fleet-claim", bool(task.get("private")))
        print(f"[fleet-sched] spawned {tid} profile={profile} pid={p.pid} "
              f"run={run_id}")
    except Exception as exc:  # noqa: BLE001
        fo.release(tid, me, "failed")
        print(f"[fleet-sched] spawn failed {tid}: {exc}")


def _balance_cfg() -> dict:
    return _read(BOT / "fleet_balance.json", {}) or {}


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 10 ** 9


def _mem_cap() -> int:
    """Reduce (never raise) concurrency as memory shrinks — dispatch less, don't kill."""
    cfg = _balance_cfg()
    floor = int(cfg.get("mem_floor_mb", os.environ.get("FLEET_MEM_FLOOR_MB", "1024")))
    step = int(cfg.get("mem_step_mb", os.environ.get("FLEET_MEM_STEP_MB", "512")))
    avail = _mem_available_mb()
    if avail < floor:
        return 0
    return max(0, avail // max(1, step))


def _root_cap(cfg: int) -> int:
    """Reserve the root for LLM traffic (Phase U4) — dispatched offload workers
    only (interactive agent sessions stay normal). Root->0 while >=2 fresh peers,
    else 1; non-root unchanged. Fail-soft to cfg."""
    try:
        import fleet_root as fr  # type: ignore
        st = fr.status()
        bal = _balance_cfg()
        return fr.effective_cap(bool(st.get("is_root")), int(st.get("fresh_count", 1)),
                                cfg, int(bal.get("root_offload_max", 0)))
    except Exception:  # noqa: BLE001
        return cfg


def _offload_max() -> int:
    # Live tunable: file wins (no restart), then env, then default; capped by memory.
    cfg = 2
    try:
        cfg = max(0, int((BOT / "fleet_offload_max").read_text().strip()))
    except Exception:
        try:
            cfg = int(os.environ.get("FLEET_OFFLOAD_MAX", "2"))
        except ValueError:
            cfg = 2
    return min(_root_cap(cfg), _mem_cap())


def _wrap_cgroup(cmd: list[str]) -> list[str]:
    """Contain a worker task in a cgroup so a runaway build is OOM-isolated
    instead of threatening the router/gateway (D-128 8.14)."""
    cfg = _balance_cfg()
    mmax = cfg.get("task_mem_max") or os.environ.get("FLEET_TASK_MEM_MAX", "")
    if not mmax or not shutil.which("systemd-run"):
        return cmd
    args = ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={mmax}"]
    # Phase U4: dispatched offload workers run at low cgroup weight so the LLM
    # proxy/gateway (CPUWeight=1000) always win CPU under contention.
    w = cfg.get("task_cpu_weight") or os.environ.get("FLEET_TASK_CPU_WEIGHT", "50")
    args += ["-p", f"CPUWeight={w}", "-p", f"IOWeight={w}"]
    mhigh = cfg.get("task_mem_high") or os.environ.get("FLEET_TASK_MEM_HIGH", "")
    if mhigh:
        args += ["-p", f"MemoryHigh={mhigh}"]
    mswap = cfg.get("task_swap_max") or os.environ.get("FLEET_TASK_SWAP_MAX", "")
    if mswap:
        args += ["-p", f"MemorySwapMax={mswap}"]
    return args + ["--"] + cmd


def _evidence_ok(rc: int, start_ts: float, tid: str, profile: str = "") -> bool:
    """A rc=0 completion is only trusted with evidence the run did real work.

    Evidence (any one is sufficient):
      * the worker profile's session store (`profiles/<p>/state.db` messages
        since the run started) — PRIMARY, because on a node whose proxy is an
        SSH tunnel to the manager (dq05), worker model calls are logged on the
        MANAGER's `zai_usage.db`, not locally;
      * a local `api_calls` row since the run started (proxy-on-this-node);
      * uncommitted changes in the task workspace.
    Fail-open when NO evidence source is readable, so a tooling gap can never
    block legitimate work; only a readable-but-empty signal blocks.
    """
    if rc != 0:
        return True  # a failure is itself an observed attempt
    import sqlite3
    checked = False
    # 1) worker session store
    if profile:
        db = HERMES / "profiles" / profile / "state.db"
        if db.exists():
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                n = c.execute("SELECT COUNT(*) FROM messages WHERE timestamp >= ?",
                              (float(start_ts),)).fetchone()[0]
                c.close()
                checked = True
                if int(n) > 0:
                    return True
            except sqlite3.Error:
                pass
    # 2) local proxy usage DB
    db = HERMES / "bot" / "zai_usage.db"
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            n = c.execute("SELECT COUNT(*) FROM api_calls WHERE ts >= ?",
                          (float(start_ts),)).fetchone()[0]
            c.close()
            checked = True
            if int(n) > 0:
                return True
        except sqlite3.Error:
            pass
    return False if checked else True


def reap(state: dict) -> None:
    me = fq._self()
    for tid, p in list(_running.items()):
        rc = p.poll()
        if rc is None:
            continue
        task = state.get("tasks", {}).get(tid, {})
        start = _run_start.pop(tid, 0.0)
        prof = _run_profile.pop(tid, "")
        # Phase V6.0c — record the run's disk footprint for disk-aware placement.
        before = _disk_before.pop(tid, None)
        if dm is not None and before is not None:
            try:
                after = dm.free_used(str(HERMES))[0]
                dm.record_run_delta(Path(dm.DB), host=me, task_id=tid,
                                    gb_used=dm.delta_gb(before, after),
                                    cls=str(task.get("resource_class") or ""))
            except Exception:  # noqa: BLE001
                pass
        verified = _evidence_ok(rc, start, tid, prof)
        done = {"type": "fleet-done", "id": tid, "actor": me, "rc": rc,
                "verified": verified, "ts": time.time()}
        fq.publish_any(done, "fleet-done", bool(task.get("private")))
        state.setdefault("tasks", {}).setdefault(tid, {})["done"] = True
        state.setdefault("tasks", {})[tid]["rc"] = rc
        state.setdefault("tasks", {})[tid]["verified"] = verified
        del _running[tid]
        _last_beat.pop(tid, None)
        fo.release(tid, me, "done" if rc == 0 else "failed")
        _write_back_local(tid, state, rc, verified)
        if rc == 0 and not verified:
            print(f"[fleet-sched] {tid} rc=0 but NO work evidence — "
                  f"marking blocked (hollow completion)")
        else:
            print(f"[fleet-sched] {tid} finished rc={rc} verified={verified}")


def _writeback_ingested(state: dict) -> None:
    """Advertiser-side: when a peer's fleet-done arrives, mark the source board."""
    for key, info in list((state.get("advertised", {}) or {}).items()):
        if not isinstance(info, dict) or info.get("written_back"):
            continue
        t = state.get("tasks", {}).get(key, {})
        if t.get("done"):
            _write_back_local(key, state, int(t.get("rc", 0) or 0),
                              bool(t.get("verified", True)))
            info["written_back"] = True


def _wb_ledger_load() -> dict:
    return _read(WRITEBACK_LEDGER, {}) or {}


def _wb_ledger_mark(key: str) -> None:
    """Record a completed write-back durably (survives state purges). Best-effort
    and bounded so the ledger can't grow without limit."""
    try:
        d = _wb_ledger_load()
        d[key] = int(time.time())
        if len(d) > 5000:
            for k in sorted(d, key=d.get)[:1000]:
                d.pop(k, None)
        WRITEBACK_LEDGER.write_text(json.dumps(d))
    except Exception:
        pass


def _write_back_local(tid: str, state: dict, rc: int, verified: bool = True) -> None:
    """On fleet-done, reflect completion on the source kanban board (D-128 8.10).
    Runs on the node that owns the board; safe/idempotent. An rc=0 run with no
    work evidence is written back as `blocked`, not `done`.

    A durable ledger guards against a replayed stale fleet-done closing a card
    that was reopened after the offload (state purges reset the in-state flag)."""
    import sqlite3
    info = (state.get("advertised", {}) or {}).get(tid) or {}
    board = info.get("board") or (state.get("tasks", {}).get(tid, {}) or {}).get("board")
    src = info.get("task") or tid.split(":")[-1]
    if not board:
        return
    wb_key = f"{board}:{src}"
    if wb_key in _wb_ledger_load():
        return  # already written back once — never re-fire on replay
    db = HERMES / "kanban" / "boards" / board / "kanban.db"
    if not db.exists():
        return
    if rc == 0 and not verified:
        new = "blocked"
    else:
        new = "done" if rc == 0 else "blocked"
    try:
        c = sqlite3.connect(str(db), timeout=5)
        row = c.execute("select status from tasks where id=?", (src,)).fetchone()
        if not row:
            c.close(); return
        if row[0] in ("done", "archived"):
            c.close(); _wb_ledger_mark(wb_key); return
        note = (f"fleet-done rc={rc} verified={verified} (offloaded); "
                f"status -> {new}")
        c.execute("insert into task_comments (task_id, author, body, created_at) "
                  "values (?,?,?,?)",
                  (src, "fleet-writeback", note, int(time.time())))
        c.execute("update tasks set status=? where id=?", (new, src))
        c.commit(); c.close()
        _wb_ledger_mark(wb_key)
        print(f"[fleet-sched] write-back {board}/{src} -> {new}")
    except Exception as exc:  # noqa: BLE001
        print(f"[fleet-sched] write-back failed {board}/{src}: {exc}")


def backfill_started(state: dict) -> int:
    """On startup, re-assert `started` claims for tasks whose worker is still
    alive (survives scheduler restarts; peers never preempt them)."""
    me = fq._self()
    n = 0
    for tid, task in (state.get("tasks", {}) or {}).items():
        ll = fo.live_lock(tid, ttl=_RUN_TTL)
        if not (ll and ll.get("pid")):
            continue
        if fq.publish_any(
                {"type": "fleet-claim", "id": tid, "actor": me,
                 "claim_ts": time.time(),
                 "headroom": fq.headroom(_read(BOT / "fleet_health.json", {})),
                 "started": True},
                "fleet-claim", bool(task.get("private"))):
            n += 1
    return n


def tick(state: dict) -> None:
    if _frozen():
        print("[fleet-sched] frozen/quarantined — skip")
        return
    adv = advertise_local(state)
    fq._ingest_into(state)
    _writeback_ingested(state)
    health = _read(BOT / "fleet_health.json", {})
    now = time.time()
    # Staleness guard (§14.21): never yield/route to a peer with stale health.
    peers = fq.load_peers(now)
    me = fq._self()
    my_fit = _read(FIT, {})
    winners = fq.resolve_claims(state.get("claims", {}))
    # Release timed-out holds BEFORE re-holding, so a peer that never acked
    # frees the card this tick rather than staying blocked indefinitely.
    release_stale_holds(state, now)
    held = apply_holds(state, winners, now, peers=peers, health=health)
    if held:
        print(f"[fleet-sched] held {held} local task(s) for offload")
    cap = _offload_max()
    # Count LIVE offload workers (including orphans from a prior scheduler
    # process) so a restart cannot oversubscribe the node.
    running_pids = {p.pid for p in _running.values()}
    in_flight = len(running_pids | _active_offload_pids())
    my_headroom = fq.headroom(health)
    for tid, task in list(state.get("tasks", {}).items()):
        if task.get("done") or tid in _running:
            continue
        winner = winners.get(tid)
        if winner == me:
            # Never execute a task this node cannot run (no fit profile / missing
            # repo). A claim can be won while permissive or on another node's
            # behalf; spawning here would fail on arrival and write the card back
            # to `blocked` (the 2026-10-09 block-loop). Skip without spawning.
            ok_fit, why_fit = fq.fit_ok(my_fit, task)
            if not ok_fit:
                print(f"[fleet-sched] {tid} won but this node is unfit "
                      f"({why_fit}) — skip")
                continue
            # D-130 rebalance: if a materially more-idle peer exists, yield this
            # queued task (never a running one) so the peer's challenge wins.
            peer = fq.should_yield(task, health, peers, my_fit, now)
            if peer:
                print(f"[fleet-sched] yielding {tid} to {peer} (rebalance)")
                continue
            ok, why = fo.can_start(tid, me, lease_winner=winner, ttl=_RUN_TTL)
            if not ok:
                print(f"[fleet-sched] {tid} owned elsewhere ({why}) — skip")
                continue
            if in_flight < cap:
                execute(task)
                in_flight += 1
            continue
        # Non-winner / unclaimed: route() may claim (and challenge an existing
        # claim) by publishing this node's headroom. Execution happens on a
        # later tick only after winning the resource lease (no double-run).
        # Never challenge a task a peer has already STARTED (no-kill).
        if winner and winner != me and _started(state, tid, winner):
            continue
        decision, reason = fq.route(task, health, peers, my_fit, now)
        task["decision"], task["decision_reason"] = decision, reason
        if decision != "claim":
            if winner and winner != me and not fo.get(tid):
                fo.record(tid, winner, "claimed")
            continue
        if _already_claimed(state, tid, me):
            continue
        claim = {"type": "fleet-claim", "id": tid, "actor": me,
                 "claim_ts": now, "headroom": my_headroom}
        if fq.publish_any(claim, "fleet-claim", bool(task.get("private"))):
            state.setdefault("claims", {}).setdefault(tid, []).append(
                {"node": me, "ts": now, "headroom": my_headroom})
            fo.record(tid, me, "claimed", repo=task.get("repo", ""))
    beat = heartbeat(state, now)
    if beat:
        print(f"[fleet-sched] heartbeat fleet-running x{beat}")
    if adv:
        print(f"[fleet-sched] advertised {adv} task(s)")
    _prune_state(state, now)


def _prune_state(state: dict, now: float) -> None:
    """Keep durable state bounded (D-126 Phase E)."""
    tasks = state.get("tasks", {})
    if len(tasks) > _MAX_STATE_TASKS:
        done = sorted((t for t in tasks.items() if t[1].get("done")),
                      key=lambda kv: kv[1].get("_ts", 0))
        for tid, _ in done[:max(0, len(tasks) - _MAX_STATE_TASKS)]:
            tasks.pop(tid, None)
    adv = state.get("advertised", {})
    if len(adv) > _MAX_STATE_TASKS:
        for key, _ in sorted(adv.items(),
                             key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0)[
                :len(adv) - _MAX_STATE_TASKS]:
            adv.pop(key, None)
    # Bound the liveness heartbeat window and the release-deny ledger.
    # Expire running entries not refreshed within _HOLD_TTL: a peer's stale
    # fleet-running heartbeat must not keep a held card blocked forever, nor
    # inflate the "running" view (observed: 111 dq05 entries ~39h old).
    runs = state.get("running", {})
    for tid in list(runs):
        if now - float((runs[tid] or {}).get("ts", 0)) > _HOLD_TTL:
            runs.pop(tid, None)
    if len(runs) > _MAX_STATE_TASKS:
        for tid, _ in sorted(runs.items(),
                             key=lambda kv: kv[1].get("ts", 0))[
                :len(runs) - _MAX_STATE_TASKS]:
            runs.pop(tid, None)
    denied = state.get("hold_denied", {})
    if len(denied) > _MAX_STATE_TASKS:
        for tid, _ in sorted(
                denied.items(),
                key=lambda kv: max((kv[1] or {}).values(), default=0))[
                :len(denied) - _MAX_STATE_TASKS]:
            denied.pop(tid, None)
    for tid in list(_last_beat):
        if tid not in _running:
            _last_beat.pop(tid, None)
    for tid in list(_run_start):
        if tid not in _running:
            _run_start.pop(tid, None)
    for tid in list(_disk_before):
        if tid not in _running:
            _disk_before.pop(tid, None)
    for tid in list(_run_profile):
        if tid not in _running:
            _run_profile.pop(tid, None)
    if not _running:
        fo.prune(ttl=_RUN_TTL, now=now)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = _read(STATE, {"tasks": {}, "claims": {}, "advertised": {}})
    state.setdefault("tasks", {}); state.setdefault("claims", {})
    state.setdefault("advertised", {})
    bf = backfill_started(state)
    if bf:
        print(f"[fleet-sched] re-asserted started lease for {bf} live task(s)")
    if args.once:
        tick(state)
        _write(STATE, state)
        return 0
    print(f"[fleet-sched] {fq._self()} loop interval={args.interval}s "
          f"max={_offload_max()} boards={_boards()}")
    try:
        while True:
            reap(state)
            tick(state)
            _write(STATE, state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
