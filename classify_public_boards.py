#!/usr/bin/env python3
"""classify_public_boards.py — decide which kanban boards target a PUBLIC repo.

Evidence-only (no board-name guessing):
  1. ~/repos/<slug>/.git origin URL;
  2. any task in the board with workspace_kind='worktree' -> git remote of its
     workspace_path.

A board is public iff at least one such remote is public (GitHub repo with
`private=false`, or an ngit remote). Everything else is private/operational and
stays node-local (never advertised to the fleet).

Writes, under --map-dir (default ~/.hermes/bot/fleet_map):
  by_node/<node>.json  {public, private, repos, ready_map, ts}   (this node only)
  public_boards.json   {slug: {...}}   union across all by_node files
  private_boards.json  {slug: {...}}   union (public wins on conflict)
  repos.json           {slug: public_repo_url}  union

Usage: classify_public_boards.py [--map-dir DIR] [--node NAME] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS = HERMES / "kanban" / "boards"
REPOS = Path(os.path.expanduser(os.environ.get("FLEET_REPOS_DIR", "~/repos")))

_GH_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")
_public_cache: dict[str, bool | None] = {}


def _read_node() -> str:
    try:
        return json.loads((HERMES / "bot" / "fleet.json").read_text()).get("node") \
            or socket.gethostname()
    except Exception:
        return socket.gethostname()


def _run(args: list[str], cwd: str | None = None, timeout: int = 20) -> str:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception:
        return ""


def is_public_remote(url: str) -> bool | None:
    """True/False, or None if undeterminable."""
    if not url:
        return None
    if url.startswith("nostr://") or "relay.ngit.dev" in url:
        return True
    m = _GH_RE.search(url)
    if not m:
        return None
    key = f"{m.group(1)}/{m.group(2)}"
    if key in _public_cache:
        return _public_cache[key]
    out = _run(["gh", "api", f"repos/{key}", "--jq", ".private"])
    res: bool | None
    if out in ("true", "false"):
        res = (out == "false")
    else:
        res = None
    _public_cache[key] = res
    return res


def _task_counts(db: Path) -> tuple[int, int]:
    if not db.exists():
        return (0, 0)
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ready = c.execute("select count(*) from tasks where status='ready'").fetchone()[0]
        todo = c.execute("select count(*) from tasks where status in ('todo','blocked')").fetchone()[0]
        c.close()
        return (ready, todo)
    except Exception:
        return (0, 0)


def _worktree_remotes(db: Path) -> list[tuple[str, str]]:
    """[(workspace_path, origin_url)] for worktree tasks in this board."""
    if not db.exists():
        return []
    out = []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute(
            "select distinct workspace_path from tasks "
            "where workspace_kind='worktree' and workspace_path is not null"
        ).fetchall()
        c.close()
    except Exception:
        return []
    for (wp,) in rows:
        if not wp or not Path(wp).exists():
            continue
        url = _run(["git", "-C", wp, "remote", "get-url", "origin"])
        if url:
            out.append((wp, url.splitlines()[0]))
    return out


def classify() -> tuple[dict, dict, dict]:
    public: dict = {}
    private: dict = {}
    reps: dict[str, str] = {}
    if not BOARDS.exists():
        return public, private, reps

    for bdir in sorted(BOARDS.iterdir()):
        db = bdir / "kanban.db"
        if not db.exists():
            continue
        slug = bdir.name
        ready, todo = _task_counts(db)
        evidence: list[str] = []
        candidates: list[str] = []

        repo_dir = REPOS / slug
        if (repo_dir / ".git").exists():
            url = _run(["git", "-C", str(repo_dir), "remote", "get-url", "origin"])
            if url:
                candidates.append(url.splitlines()[0])
                evidence.append(f"repos/{slug}:{url.splitlines()[0]}")

        for wp, url in _worktree_remotes(db):
            candidates.append(url)
            evidence.append(f"worktree:{wp}:{url}")

        pub_url = None
        for url in candidates:
            if is_public_remote(url) is True:
                pub_url = url
                break

        if pub_url:
            public[slug] = {"repo": pub_url, "evidence": evidence,
                            "ready": ready, "todo": todo}
            reps[slug] = pub_url
        else:
            reason = "no-public-remote" if candidates else "no-repo"
            private[slug] = {"reason": reason, "evidence": evidence,
                             "ready": ready, "todo": todo}
    return public, private, reps


def union_nodes(docs: list[dict]) -> tuple[dict, dict, dict]:
    """Pure: union per-node classification docs -> (public, private, repos).

    Privacy rule (fail-closed): a board is public only if some node proved a
    public remote for it. Public wins over private on conflict (the proving node
    had evidence); a board that is only ever private never enters the public set.
    """
    u_public: dict = {}
    u_private: dict = {}
    u_repos: dict = {}
    for d in docs:
        if not isinstance(d, dict):
            continue
        u_public.update(d.get("public", {}) or {})
        u_repos.update(d.get("repos", {}) or {})
    for d in docs:
        if not isinstance(d, dict):
            continue
        for s, v in (d.get("private", {}) or {}).items():
            if s not in u_public:
                u_private[s] = v
    for s in u_public:
        u_private.pop(s, None)
    return u_public, u_private, u_repos


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-dir", default=str(HERMES / "bot" / "fleet_map"))
    ap.add_argument("--node", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.map_dir)
    (out / "by_node").mkdir(parents=True, exist_ok=True)

    node = args.node or _read_node()
    public, private, reps = classify()
    ready_map = {s: v["ready"] for s, v in public.items()}
    (out / "by_node" / f"{node}.json").write_text(json.dumps({
        "node": node, "ts": int(time.time()), "public": public,
        "private": private, "repos": reps, "ready_map": ready_map}, indent=1))

    # Union across all node files (public wins over private on conflict).
    docs = []
    for f in sorted((out / "by_node").glob("*.json")):
        try:
            docs.append(json.loads(f.read_text()))
        except Exception:
            continue
    u_public, u_private, u_repos = union_nodes(docs)

    # Hard fail-closed re-verification (8.8): NEVER publish a board/repo unless a
    # public remote is provably public *now*. This defends against stale node data
    # and repos that were flipped to private after a previous classification.
    v_public: dict = {}
    v_repos: dict = {}
    for s, v in u_public.items():
        url = v.get("repo") or u_repos.get(s, "")
        if is_public_remote(url) is True:
            v_public[s] = v
            if s in u_repos:
                v_repos[s] = u_repos[s]
        else:
            u_private.setdefault(s, {"reason": "unverified-public",
                                     "evidence": v.get("evidence", []),
                                     "ready": v.get("ready", 0), "todo": v.get("todo", 0)})
    u_public, u_repos = v_public, v_repos
    for s in u_public:
        u_private.pop(s, None)

    (out / "public_boards.json").write_text(json.dumps(u_public, indent=1))
    (out / "private_boards.json").write_text(json.dumps(u_private, indent=1))
    (out / "repos.json").write_text(json.dumps(u_repos, indent=1))

    active = {s: v for s, v in u_public.items() if v["ready"] + v["todo"] > 0}
    if args.json:
        print(json.dumps({"node": node, "public": len(u_public),
                          "private": len(u_private), "active_public": len(active)},
                         indent=1))
    else:
        print(f"[{node}] public={len(public)} private={len(private)} | "
              f"fleet public={len(u_public)} private={len(u_private)} "
              f"active_public={len(active)}")
        for s in sorted(active):
            v = active[s]
            print(f"  PUBLIC  {s:32} ready={v['ready']} todo={v['todo']} "
                  f"repo={v['repo']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
