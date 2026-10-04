#!/usr/bin/env python3
"""fleet_ownership.py — cross-node task ownership ledger + local locks (D-126).

The deterministic Nostr lease (fleet_queue.resolve_claims) decides the winner in
real time. This module adds a durable, auditable record of *which machine is
handling which task* plus a local lock so a node never double-starts a task.

Ledger: ~/.hermes/bot/ownership.json
  {task_id: {node, status, repo, branch, run_id, ts, updated}}

Locks:  ~/.hermes/bot/.locks/<task_id>.json
  atomic O_EXCL marker; stale locks (older than TTL) are reclaimable.

Status values: claimed | running | done | failed | released.

CLI:
  fleet_ownership.py status [--json]
  fleet_ownership.py record <task_id> <node> <status> [--repo R] [--branch B] [--run-id ID]
  fleet_ownership.py release <task_id> <node> [status]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
LEDGER = BOT / "ownership.json"
LOCK_DIR = BOT / ".locks"
DEFAULT_TTL = 3600


def _read(p: Path, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d


def _write_atomic(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


def _ledger() -> dict:
    return _read(LEDGER, {})


def get(task_id: str) -> dict:
    return _ledger().get(task_id, {}) or {}


def _age(rec: dict, now: float) -> float:
    return now - float(rec.get("updated", rec.get("ts", 0)) or 0)


def can_start(task_id: str, node: str, lease_winner: str | None = None,
              ttl: int = DEFAULT_TTL, now: float | None = None) -> tuple[bool, str]:
    """May `node` start `task_id`? Considers the lease + shared ledger."""
    now = now if now is not None else time.time()
    if lease_winner and lease_winner != node:
        return False, f"leased:{lease_winner}"
    rec = get(task_id)
    if rec and rec.get("node") and rec["node"] != node and \
            rec.get("status") in ("running", "claimed") and _age(rec, now) < ttl:
        return False, f"owned-by:{rec['node']}({rec.get('status')})"
    return True, ""


def record(task_id: str, node: str, status: str, repo: str = "",
           branch: str = "", run_id: str = "", now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    led = _ledger()
    rec = led.get(task_id, {}) or {}
    rec.update({"node": node, "status": status, "updated": now})
    rec.setdefault("ts", now)
    if repo:
        rec["repo"] = repo
    if branch:
        rec["branch"] = branch
    if run_id:
        rec["run_id"] = run_id
    led[task_id] = rec
    _write_atomic(LEDGER, led)
    return rec


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def lock_path(task_id: str) -> Path:
    safe = task_id.replace("/", "_")
    return LOCK_DIR / f"{safe}.json"


def acquire_lock(task_id: str, node: str, run_id: str = "",
                 ttl: int = DEFAULT_TTL, now: float | None = None,
                 pid: int | None = None) -> bool:
    """Atomically take the local lock. Steals a stale lock (no live pid)."""
    now = now if now is not None else time.time()
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    p = lock_path(task_id)
    payload = json.dumps({"node": node, "run_id": run_id, "ts": now, "pid": pid})
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        return True
    except FileExistsError:
        rec = _read(p, {}) or {}
        dead_pid = rec.get("pid") is not None and not _pid_alive(rec.get("pid"))
        if rec.get("node") == node or _age(rec, now) >= ttl or dead_pid:
            try:
                p.write_text(payload)
                return True
            except OSError:
                return False
        return False


def set_lock_pid(task_id: str, pid: int) -> None:
    p = lock_path(task_id)
    rec = _read(p, {}) or {}
    rec["pid"] = pid
    try:
        p.write_text(json.dumps(rec))
    except OSError:
        pass


def live_lock(task_id: str, ttl: int = DEFAULT_TTL,
              now: float | None = None) -> dict | None:
    """Return the lock record if a worker for this task is still alive."""
    now = now if now is not None else time.time()
    rec = _read(lock_path(task_id), {}) or {}
    if not rec:
        return None
    if rec.get("pid") is not None:
        return rec if _pid_alive(rec.get("pid")) else None
    return rec if _age(rec, now) < ttl else None


def release(task_id: str, node: str, status: str = "done",
            now: float | None = None) -> None:
    record(task_id, node, status, now=now)
    p = lock_path(task_id)
    try:
        rec = _read(p, {}) or {}
        if rec.get("node") in (None, node):
            p.unlink(missing_ok=True)
    except OSError:
        pass


def prune(ttl: int = DEFAULT_TTL, now: float | None = None) -> int:
    """Drop finished ledger records and stale locks; returns count pruned."""
    now = now if now is not None else time.time()
    led = _ledger()
    keep = {k: v for k, v in led.items()
            if v.get("status") not in ("done", "failed", "released")
            or _age(v, now) < ttl}
    n = len(led) - len(keep)
    if n:
        _write_atomic(LEDGER, keep)
    if LOCK_DIR.exists():
        for f in LOCK_DIR.glob("*.json"):
            rec = _read(f, {}) or {}
            if _age(rec, now) >= ttl:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("record")
    p.add_argument("task_id"); p.add_argument("node"); p.add_argument("status")
    p.add_argument("--repo", default=""); p.add_argument("--branch", default="")
    p.add_argument("--run-id", default="")
    p = sub.add_parser("release")
    p.add_argument("task_id"); p.add_argument("node")
    p.add_argument("status", nargs="?", default="done")
    p = sub.add_parser("prune")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        led = _ledger()
        if args.json:
            print(json.dumps(led, indent=1))
        else:
            running = {k: v for k, v in led.items()
                       if v.get("status") in ("running", "claimed")}
            print(f"ownership: {len(led)} records, {len(running)} active")
            for k, v in sorted(running.items()):
                print(f"  {k:28} {v.get('node','?'):10} {v.get('status','?'):8} "
                      f"repo={v.get('repo','')}")
        return 0
    if args.cmd == "record":
        print(json.dumps(record(args.task_id, args.node, args.status,
                                repo=args.repo, branch=args.branch,
                                run_id=args.run_id)))
        return 0
    if args.cmd == "release":
        release(args.task_id, args.node, args.status)
        print("released")
        return 0
    if args.cmd == "prune":
        print(f"pruned {prune()}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
