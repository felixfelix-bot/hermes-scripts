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
            Self-limiting: after a successful push, if the mirror has grown past
            the auto-compact thresholds (commits > 200, packs > 10, or size >
            200 MB) it squashes the local history to ONE commit (the current
            tree) and force-pushes the result. If ``pull --rebase`` fails
            because a peer rewrote the remote history (non-fast-forward), it
            falls back to ``git fetch`` + ``git reset --hard origin/<branch>``
            — safe because the worktree is regenerated from board DBs on every
            export — and continues, logging which path was taken.
  compact — collapse the mirror history into ONE commit holding the CURRENT
            tree, using bare-repo-safe plumbing (``git commit-tree`` +
            ``git update-ref`` + ``git reflog expire`` + ``git gc``). Never
            uses ``git checkout``, so it works on a bare repo (the peer-host
            mirror has no worktree). Prints before/after object+pack+size.

The kanban state repo is separate from hermes-orchestration so task churn never
pollutes code history. History of a state mirror has no value — only the
current tree matters (the 2026-10-06 2.5 GB / 18M-object bloat was pure append
history), hence compact + auto-compact. Config: ``~/.hermes/bot/kanban_git.json``
``{"repo": "~/hermes-kanban", "boards_root": "~/.hermes/kanban/boards"}``.

CLI:
  kanban_git.py export  [--board B] [--repo R] [--boards-root P]
  kanban_git.py claim   <board> <task_id> [--node N] [--ttl S] [--repo R]
  kanban_git.py import  [--board B] [--apply] [--repo R] [--boards-root P]
  kanban_git.py sync    [--board B] [--repo R] [--boards-root P]
  kanban_git.py compact [--repo R]
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
# Runtime-only columns never materialized from the shared repo: a task pulled
# onto a fresh node must be unclaimed/unstarted locally.
RUNTIME_NULL_COLS = {"worker_pid", "claim_lock", "claim_expires",
                     "last_heartbeat_at", "current_run_id"}


def _hermes_bin() -> str:
    b = os.environ.get("HERMES_BIN")
    if b and Path(b).exists():
        return b
    # Install layouts differ across nodes (venv/bin, nested .hermes/bin, ~/.local/bin).
    for p in (
        HERMES / "hermes-agent" / "venv" / "bin" / "hermes",
        HERMES / "hermes-agent" / ".hermes" / "bin" / "hermes",
        Path(os.path.expanduser("~/.local/bin/hermes")),
        Path("/usr/local/bin/hermes"),
    ):
        if p.exists():
            return str(p)
    return "hermes"


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
            # No per-export timestamp here: a volatile ``_exported_at`` made
            # every sync rewrite all ~6.5k task files (17M-object history
            # bloat, 2026-10-06). The sync time lives in the commit message.
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


def materialize(repo: Path, boards_root: Path, only: str | None = None,
                apply: bool = False) -> dict:
    """Create boards + tasks on this node from the shared repo (ADR-013).

    The counterpart to ``export`` for a node that does not have the board yet:
    creates the board via the official CLI (real schema/registry) and inserts
    tasks with ``INSERT OR IGNORE`` (local edits never clobbered). Runtime-only
    columns are forced inactive so a pulled task is never pre-claimed.
    Dry-run by default. Returns ``{boards_created, tasks_created, skipped}``.
    """
    stats = {"boards_created": 0, "tasks_created": 0, "skipped": 0}
    root = repo / "boards"
    if not root.is_dir():
        return stats
    for bdir in sorted(root.iterdir()):
        slug = bdir.name
        if not bdir.is_dir() or (only and slug != only):
            continue
        tdir = bdir / "tasks"
        if not tdir.is_dir():
            continue
        target = boards_root / slug
        db = target / "kanban.db"
        board_json = _read_json(bdir / "board.json", {}) or {}
        if not db.exists():
            stats["boards_created"] += 1
            if not apply:
                stats["tasks_created"] += len(list(tdir.glob("*.json")))
                continue
            name = board_json.get("name") or slug
            cmd = [_hermes_bin(), "kanban", "boards", "create", slug, "--name", name]
            if board_json.get("description"):
                cmd += ["--description", str(board_json["description"])]
            if board_json.get("default_workdir"):
                cmd += ["--default-workdir", str(board_json["default_workdir"])]
            subprocess.run(cmd, capture_output=True, text=True)
            if not db.exists():  # fallback: init the schema directly
                target.mkdir(parents=True, exist_ok=True)
                subprocess.run([_hermes_bin(), "kanban", "--board", slug, "init"],
                               capture_output=True, text=True)
            if board_json:
                _write_json_atomic(target / "board.json", board_json)
        if not db.exists():
            stats["skipped"] += 1
            continue
        con = sqlite3.connect(str(db))
        try:
            # Never collide with the gateway's own writer: WAL + a long busy
            # timeout. A fresh board is created then populated in one pass; the
            # gateway may open it concurrently, so these pragmas matter.
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA journal_mode=WAL")
            cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
            for f in sorted(tdir.glob("*.json")):
                rec = _read_json(f, {}) or {}
                if not rec.get("id") or not rec.get("title") or not rec.get("status"):
                    stats["skipped"] += 1
                    continue
                data = {k: v for k, v in rec.items()
                        if k in cols and k not in RUNTIME_NULL_COLS}
                if "workspace_kind" in cols and not data.get("workspace_kind"):
                    data["workspace_kind"] = "scratch"
                if not apply:
                    exists = con.execute("SELECT 1 FROM tasks WHERE id=?",
                                         (rec["id"],)).fetchone()
                    if not exists:
                        stats["tasks_created"] += 1
                    continue
                placeholders = ",".join("?" for _ in data)
                cur = con.execute(
                    f"INSERT OR IGNORE INTO tasks({','.join(data)}) VALUES({placeholders})",
                    list(data.values()))
                if cur.rowcount:
                    stats["tasks_created"] += 1
                # Heal urgency metadata on a task that was inserted before the
                # urgency columns existed (INSERT OR IGNORE skips it), and
                # release cards parked by the urgency_required gate solely
                # because they were unclassified.
                if "urgency" in cols and rec.get("urgency") is not None:
                    sets = {"urgency": rec.get("urgency")}
                    for k in ("urgency_deadline", "urgency_set_at", "urgency_source"):
                        if k in cols and rec.get(k) is not None:
                            sets[k] = rec.get(k)
                    cur = con.execute(
                        "UPDATE tasks SET " + ",".join(f"{k}=?" for k in sets)
                        + " WHERE id=? AND urgency IS NULL",
                        (*sets.values(), rec["id"]))
                    if cur.rowcount:
                        stats["healed"] = stats.get("healed", 0) + 1
                        if "urgency_source" in cols:
                            con.execute(
                                "UPDATE tasks SET status='ready' "
                                "WHERE id=? AND status='scheduled' "
                                "AND urgency_source='urgency-unclassified'",
                                (rec["id"],))
            if apply:
                con.commit()
        except sqlite3.Error:
            stats["skipped"] += 1
        finally:
            con.close()
    return stats


# Hard wall-clock bound for every git invocation. The 2026-10-06 x240 incident
# was a `git pull --rebase` that wedged for >1h holding a rebase + auto-gc,
# feeding an inode blowup. A network git op must never run unbounded.
GIT_TIMEOUT_S = int(os.environ.get("KANBAN_GIT_TIMEOUT_S", "120"))


def _git(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = GIT_TIMEOUT_S
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        err = exc.stderr
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return subprocess.CompletedProcess(
            ["git", "-C", str(repo), *args], 124,
            out or "",
            (err or "") + f"\nkanban_git: git {' '.join(args)} timed out after {timeout}s")


def sync(repo: Path, boards_root: Path, only: str | None = None) -> int:
    if not (repo / ".git").exists():
        print(f"kanban_git: {repo} is not a git repo", file=sys.stderr)
        return 2
    # Materialize any board this node is missing before exporting (ADR-013).
    materialize(repo, boards_root, only, apply=True)
    written = export(repo, boards_root, only)
    _git(repo, "add", "-A")
    if _git(repo, "diff", "--cached", "--quiet").returncode != 0:
        _git(repo, "-c", "user.name=hermes-kanban", "-c",
             "user.email=kanban@orangesync.tech",
             "commit", "-q", "-m", f"kanban: sync {time.strftime('%FT%TZ', time.gmtime())}")
    # Pull explicitly from origin/<branch>: a fresh node has no upstream tracking,
    # so a bare `git pull` fails and the node would diverge on an empty history.
    _git(repo, "fetch", "origin")  # best-effort
    branch = (_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout or "master").strip() or "master"
    path = "rebase"
    r = _git(repo, "pull", "--rebase", "--autostash", "origin", branch)  # best-effort
    if r.returncode != 0 and (
        (repo / ".git" / "rebase-merge").exists()
        or (repo / ".git" / "rebase-apply").exists()
    ):
        # A failed/timed-out rebase leaves the repo mid-rebase and blocks every
        # later sync. Abort it (--autostash restores the working tree) so the
        # next run starts clean.
        _git(repo, "rebase", "--abort", timeout=30)
        print(f"kanban_git: aborted a stuck rebase in {repo}", file=sys.stderr)
    if r.returncode != 0:
        # Divergence fallback: the remote history was rewritten by a peer's
        # `compact` (non-fast-forward). Rebase cannot apply local history onto a
        # rewritten base, so discard the local (regenerated) history and adopt
        # the remote tip. Safe: the working tree is rebuilt from board DBs every
        # export, so `git reset --hard` loses nothing but append commits.
        fr = _git(repo, "reset", "--hard", f"origin/{branch}")
        if fr.returncode == 0:
            path = "reset"
            print(f"kanban_git: pull --rebase diverged; fell back to "
                  f"fetch+reset --hard origin/{branch}", file=sys.stderr)
    _git(repo, "push", "origin", branch)  # best-effort
    # Self-limiting auto-compact: squash the mirror after a successful push when
    # it has grown past the bloat thresholds (2026-10-06 x240 incident).
    if should_auto_compact(repo):
        print(f"kanban_git: auto-compact threshold exceeded; compacting {repo}",
              file=sys.stderr)
        compact(repo)
    return written


def _gitdir(repo: Path) -> Path:
    """Return the git directory for a repo, bare or not.

    A non-bare repo has ``repo/.git``; a bare repo (the peer-host mirror) has its
    git files directly under ``repo`` (a ``HEAD`` file, no ``.git``).
    """
    if (repo / ".git").exists():
        return repo / ".git"
    if (repo / "HEAD").exists():
        return repo
    return repo / ".git"  # fallback: caller will fail cleanly on is_git_repo


def _is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists() or (repo / "HEAD").exists()


def compact(repo: Path) -> int:
    """Collapse the mirror history into ONE commit holding the CURRENT tree.

    Bare-repo-safe plumbing (``git commit-tree`` + ``git update-ref``), never
    ``git checkout`` — the peer-host mirror has no worktree. History of a state
    mirror has no value; only the current tree matters. Returns 0 on success,
    non-zero on failure.
    """
    if not _is_git_repo(repo):
        print(f"kanban_git: {repo} is not a git repo", file=sys.stderr)
        return 2
    branch = (_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout
              or "master").strip() or "master"
    tree = (_git(repo, "rev-parse", f"{branch}^{{tree}}").stdout or "").strip()
    if not tree:
        print(f"kanban_git: compact: no tree for branch {branch!r}", file=sys.stderr)
        return 1
    before = _repo_stats(repo)
    new_sha = (_git(repo, "-c", "user.name=hermes-kanban", "-c",
                    "user.email=kanban@orangesync.tech",
                    "commit-tree", tree, "-m",
                    f"board sync (history squashed {time.strftime('%FT%TZ', time.gmtime())})")
               .stdout or "").strip()
    if not new_sha:
        print("kanban_git: compact: commit-tree produced no commit", file=sys.stderr)
        return 1
    _git(repo, "update-ref", f"refs/heads/{branch}", new_sha)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now", "--quiet")
    after = _repo_stats(repo)
    print(f"kanban_git: compact {branch}: "
          f"objects {before['objects']}->{after['objects']}, "
          f"packs {before['packs']}->{after['packs']}, "
          f"size {before['size_mb']}->{after['size_mb']} MB")
    return 0


def _repo_stats(repo: Path) -> dict:
    """Lightweight repo weight snapshot: loose+packed objects, packs, size MB."""
    stats = {"objects": "?", "packs": "?", "size_mb": "?"}
    p = _git(repo, "count-objects", "-vH", timeout=60)
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("count:"):
            stats["objects"] = line.split(":", 1)[1].strip()
        elif line.startswith("in-pack:"):
            stats["objects"] = line.split(":", 1)[1].strip()
        elif line.startswith("packs:"):
            stats["packs"] = line.split(":", 1)[1].strip()
    try:
        stats["size_mb"] = str(round(_gitdir_size_mb(repo), 1))
    except OSError:
        stats["size_mb"] = "?"
    return stats


def _gitdir_size_mb(repo: Path) -> float:
    """Total size of the repo's git dir in MiB (loose objects + packs)."""
    gitdir = _gitdir(repo)
    total = 0
    for root, _dirs, files in os.walk(gitdir):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def _commit_count(repo: Path) -> int:
    """Number of commits reachable from HEAD (mirror history length)."""
    p = _git(repo, "rev-list", "--count", "HEAD", timeout=60)
    try:
        return int((p.stdout or "").strip() or 0)
    except ValueError:
        return 0


def _pack_count(repo: Path) -> int:
    """Number of pack files in the repo (a proxy for gc churn)."""
    packdir = _gitdir(repo) / "objects" / "pack"
    if not packdir.is_dir():
        return 0
    return len(list(packdir.glob("*.pack")))


# Auto-compact thresholds (2026-10-06 x240): a state mirror should stay tiny.
AUTO_COMPACT_COMMITS = 200
AUTO_COMPACT_PACKS = 10
AUTO_COMPACT_SIZE_MB = 200.0


def should_auto_compact(repo: Path) -> bool:
    """True when the mirror has grown past any auto-compact threshold.

    Pure decision over measured repo weight; the thresholds are module constants
    so the boundary tests can pin them exactly.
    """
    try:
        size_mb = _gitdir_size_mb(repo)
    except OSError:
        size_mb = 0.0
    return (_commit_count(repo) > AUTO_COMPACT_COMMITS
            or _pack_count(repo) > AUTO_COMPACT_PACKS
            or size_mb > AUTO_COMPACT_SIZE_MB)


def maintenance(repo: Path) -> int:
    """Reclaim disk + inodes in the kanban git repo.

    2026-10-06 x240: 4.44M loose objects (~1.9M inodes) plus a stale gc.pid from
    a crashed gc wedged the repo and exhausted the filesystem's inodes. Safe on
    a live repo: clears a STALE lock, drops leftover temp packs, then runs a
    bounded ``git gc --prune=now``.
    """
    if not (repo / ".git").exists():
        print(f"kanban_git: {repo} is not a git repo", file=sys.stderr)
        return 2
    gitdir = repo / ".git"

    def _count_loose() -> str:
        p = _git(repo, "count-objects", "-v", timeout=60)
        for line in (p.stdout or "").splitlines():
            if line.startswith("count:"):
                return line.split(":", 1)[1].strip()
        return "?"

    before = _count_loose()

    gc_pid = gitdir / "gc.pid"
    if gc_pid.exists():
        try:
            age = time.time() - gc_pid.stat().st_mtime
        except OSError:
            age = 0.0
        if age > 300:
            gc_pid.unlink(missing_ok=True)
            print(f"kanban_git: removed stale gc.pid (age {int(age)}s)")
        else:
            print(f"kanban_git: gc.pid fresh (age {int(age)}s); skipping gc")
            return 0

    packdir = gitdir / "objects" / "pack"
    for pat in ("tmp_pack_*", ".tmp-*"):
        for p in list(packdir.glob(pat)):
            try:
                p.unlink()
                print(f"kanban_git: removed leftover {p.name}")
            except OSError:
                pass

    r = _git(repo, "gc", "--prune=now", "--quiet",
             timeout=int(os.environ.get("KANBAN_GIT_GC_TIMEOUT_S", "1800")))
    if r.returncode != 0:
        print(f"kanban_git: gc failed rc={r.returncode}: {(r.stderr or '').strip()[:200]}",
              file=sys.stderr)
        return 1
    print(f"kanban_git: gc complete (loose {before} -> {_count_loose()})")
    return 0


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--boards-root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "import", "sync", "materialize"):
        s = sub.add_parser(name)
        s.add_argument("--board", default=None)
    sub.choices["import"].add_argument("--apply", action="store_true")
    sub.choices["materialize"].add_argument("--apply", action="store_true")
    sub.add_parser("maintenance")
    sub.add_parser("compact")
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
    if args.cmd == "materialize":
        st = materialize(repo, broot, args.board, apply=args.apply)
        print(f"kanban_git: materialize (dry-run)" if not args.apply else "kanban_git: materialize")
        print(f"  boards_created={st['boards_created']} tasks_created={st['tasks_created']} "
              f"healed={st.get('healed',0)} skipped={st['skipped']}")
        return 0
    if args.cmd == "sync":
        n = sync(repo, broot, args.board)
        print(f"kanban_git: synced ({n} changed file(s))")
        return 0
    if args.cmd == "maintenance":
        return maintenance(repo)
    if args.cmd == "compact":
        return compact(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
