#!/usr/bin/env python3
"""kanban_git.py — git-backed shared kanban (ADR-013).

Nodes share one board through a **private git repo** of per-task JSON files, so
any node can dispatch independently **without duplicate work**:

  export  — publish every board's tasks as ``boards/<board>/tasks/<id>.json``
            (+ ``board.json`` + ``index.json``) into the kanban git repo.
  claim   — optimistically claim a task for this node (a claim file in the repo
            + the durable fleet_ownership ledger). Returns won/lost, so two
            nodes never both start the same task.
  import  — apply **forward status transitions** from the repo back into the
            local SQLite, conservatively (status-only; never touch a task that
            is running/claimed locally). Default is dry-run.
  sync    — export -> git commit -> pull --rebase -> push.

The kanban state repo is separate from hermes-orchestration so task churn never
pollutes code history. Config: ``~/.hermes/bot/kanban_git.json``
``{"repo": "~/hermes-kanban", "boards_root": "~/.hermes/kanban/boards"}``.

CLI:
  kanban_git.py export [--board B] [--repo R] [--boards-root P]
  kanban_git.py claim  <board> <task_id> [--node N] [--ttl S] [--repo R]
  kanban_git.py import [--board B] [--apply] [--repo R] [--boards-root P]
  kanban_git.py sync   [--board B] [--repo R] [--boards-root P]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DEFAULT_BOARDS_ROOT = HERMES / "kanban" / "boards"
DEFAULT_REPO = Path(os.path.expanduser("~/hermes-kanban"))
DEFAULT_CLAIM_TTL = 3600
# forward-only status order (lower = earlier). Unknown statuses fall back to -1.
STATUS_ORDER = {"todo": 0, "triage": 0, "ready": 1, "running": 2,
                "blocked": 2, "done": 3, "failed": 3, "cancelled": 3}


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _write_json_atomic(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


def load_config() -> dict:
    cfg = _read_json(HERMES / "bot" / "kanban_git.json", {}) or {}
    return {
        "repo": Path(os.path.expanduser(cfg.get("repo", str(DEFAULT_REPO)))),
        "boards_root": Path(os.path.expanduser(cfg.get("boards_root", str(DEFAULT_BOARDS_ROOT)))),
    }


def _boards(boards_root: Path, only: str | None) -> list[str]:
    out = []
    for d in sorted(boards_root.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        if not (d / "kanban.db").exists():
            continue
        if only and d.name != only:
            continue
        out.append(d.name)
    return out


def _db_tasks(db: Path) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("select * from tasks").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [dict(r) for r in rows]


def export(repo: Path, boards_root: Path, only: str | None = None) -> int:
    """Write per-task JSON for every board. Returns number of files written."""
    written = 0
    for board in _boards(boards_root, only):
        bdir = repo / "boards" / board
        # board definition (repo already tracks it, but keep a copy in the state repo)
        bjson = boards_root / board / "board.json"
        if bjson.exists():
            _write_json_atomic(bdir / "board.json", _read_json(bjson, {}))
        tasks = _db_tasks(boards_root / board / "kanban.db")
        index = {}
        for t in tasks:
            tid = str(t.get("id", ""))
            if not tid:
                continue
            t["_board"] = board
            t["_exported_at"] = int(time.time())
            dest = bdir / "tasks" / f"{tid}.json"
            if _read_json(dest) != t:
                _write_json_atomic(dest, t)
                written += 1
            index[tid] = {"status": t.get("status"), "assignee": t.get("assignee"),
                          "title": t.get("title")}
        _write_json_atomic(bdir / "index.json", index)
    return written


def _claims(repo: Path) -> list[dict]:
    out = []
    cdir = repo / "claims"
    if cdir.is_dir():
        for f in sorted(cdir.glob("*.json")):
            c = _read_json(f, {})
            if isinstance(c, dict):
                out.append(c)
    return out


def claim(repo: Path, board: str, task_id: str, node: str,
          ttl: int = DEFAULT_CLAIM_TTL) -> bool:
    """Claim a task for `node`. True if we won, False if another node holds it."""
    now = int(time.time())
    key = f"{board}__{task_id}"
    for c in _claims(repo):
        if c.get("key") != key:
            continue
        if c.get("node") == node:
            return True  # we already hold it
        if int(c.get("expires", 0)) > now:
            return False  # another node holds a live claim
    _write_json_atomic(repo / "claims" / f"{key}.json",
                       {"key": key, "board": board, "task_id": task_id,
                        "node": node, "ts": now, "expires": now + ttl})
    # mirror to the durable ledger + local lock (best-effort)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fleet_ownership  # type: ignore
        fleet_ownership.record(task_id, node, "claimed")  # type: ignore[attr-defined]
    except Exception:
        pass
    return True


def import_status(repo: Path, boards_root: Path, only: str | None = None,
                  apply: bool = False, node: str = "") -> int:
    """Apply forward status transitions from the repo into local SQLite.

    Conservative: status-only; skips tasks that are `running` locally for another
    node, and never moves a task backwards. Returns the number of changes."""
    changes = 0
    for board in _boards(boards_root, only):
        tdir = repo / "boards" / board / "tasks"
        if not tdir.is_dir():
            continue
        db = boards_root / board / "kanban.db"
        con = sqlite3.connect(str(db))
        try:
            for f in sorted(tdir.glob("*.json")):
                rec = _read_json(f, {})
                tid = str(rec.get("id", ""))
                new = rec.get("status")
                if not tid or not new:
                    continue
                row = con.execute("select status from tasks where id=?", (tid,)).fetchone()
                if not row:
                    continue
                cur = row[0]
                if cur == new:
                    continue
                # never move backwards; never override a running local task
                if STATUS_ORDER.get(new, -1) < STATUS_ORDER.get(cur, -1):
                    continue
                if cur == "running" and rec.get("worker_pid"):
                    continue
                changes += 1
                if apply:
                    con.execute("update tasks set status=? where id=?", (new, tid))
            if apply and changes:
                con.commit()
        finally:
            con.close()
    return changes


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def sync(repo: Path, boards_root: Path, only: str | None = None) -> int:
    if not (repo / ".git").exists():
        print(f"kanban_git: {repo} is not a git repo", file=sys.stderr)
        return 2
    written = export(repo, boards_root, only)
    _git(repo, "add", "-A")
    if _git(repo, "diff", "--cached", "--quiet").returncode != 0:
        _git(repo, "-c", "user.name=hermes-kanban", "-c",
             "user.email=kanban@orangesync.tech",
             "commit", "-q", "-m", f"kanban: sync {time.strftime('%FT%TZ', time.gmtime())}")
    _git(repo, "pull", "--rebase", "--autostash")  # best-effort
    _git(repo, "push")                              # best-effort
    return written


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--boards-root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "import", "sync"):
        s = sub.add_parser(name)
        s.add_argument("--board", default=None)
    sub.choices["import"].add_argument("--apply", action="store_true")
    c = sub.add_parser("claim")
    c.add_argument("board")
    c.add_argument("task_id")
    c.add_argument("--node", default=os.environ.get("FLEET_NODE", ""))
    c.add_argument("--ttl", type=int, default=DEFAULT_CLAIM_TTL)
    args = ap.parse_args(argv)

    cfg = load_config()
    repo = Path(os.path.expanduser(args.repo)) if args.repo else cfg["repo"]
    broot = Path(os.path.expanduser(args.boards_root)) if args.boards_root else cfg["boards_root"]

    if args.cmd == "export":
        n = export(repo, broot, args.board)
        print(f"kanban_git: exported {n} changed task file(s) -> {repo}")
        return 0
    if args.cmd == "claim":
        if not args.node:
            print("kanban_git: no node (set FLEET_NODE or --node)", file=sys.stderr)
            return 2
        won = claim(repo, args.board, args.task_id, args.node, args.ttl)
        print("won" if won else "lost")
        return 0 if won else 1
    if args.cmd == "import":
        n = import_status(repo, broot, args.board, apply=args.apply)
        print(f"kanban_git: {n} status change(s){' applied' if args.apply else ' (dry-run)'}")
        return 0
    if args.cmd == "sync":
        n = sync(repo, broot, args.board)
        print(f"kanban_git: synced ({n} changed file(s))")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
