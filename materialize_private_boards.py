#!/usr/bin/env python3
"""materialize_private_boards.py — give DQ05 a real (private) repo per active
private/operational board so it can offload that work (D-128 8.9).

For each selected board:
  - repo already cloned locally           -> register
  - private GitHub repo exists            -> clone
  - otherwise                             -> create a PRIVATE repo seeded from
                                             the lane and register

Writes:
  ~/.hermes/bot/fleet_map/repos.json               (merged board -> url)
  ~/.hermes/bot/fleet_map/private_offload_boards.json  (opt-in list for the bus)

Default is a DRY RUN. Use --apply to create/clone. Active-first by ready+todo.

Usage:
  materialize_private_boards.py [--map-dir DIR] [--limit N] [--only a,b]
                                [--org felixfelix-bot] [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
BOT = HERMES / "bot"
REPOS = HOME / "repos"
DEFAULT_MAP = BOT / "fleet_map"
DEFAULT_ORG = "felixfelix-bot"

PRIORITY = ["plebeian-pr-reviews", "net4sats-mvp-v2", "merchant-routing",
            "llm-routing", "router-quality", "house-keeping"]


def _run(args, cwd=None, timeout=120) -> tuple[int, str]:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write(p: Path, v):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


def select_boards(private: dict, limit: int, only: list[str]) -> list[str]:
    if only:
        return [b for b in only if b in private]
    active = {b: v for b, v in private.items()
              if (v.get("ready", 0) + v.get("todo", 0)) > 0}
    ordered = [b for b in PRIORITY if b in active] + \
              sorted([b for b in active if b not in PRIORITY],
                     key=lambda b: -(active[b].get("ready", 0) + active[b].get("todo", 0)))
    return ordered[:limit]


def ensure_repo(org: str, slug: str, apply: bool) -> tuple[str, str]:
    dest = REPOS / slug
    if (dest / ".git").exists():
        url = _run(["git", "-C", str(dest), "remote", "get-url", "origin"])[1]
        return "exists", url
    # does a private GitHub repo already exist?
    rc, _ = _run(["gh", "repo", "view", f"{org}/{slug}", "--json", "name"])
    if rc == 0:
        if not apply:
            return "would-clone", f"https://github.com/{org}/{slug}.git"
        rc2, out = _run(["git", "clone", f"https://github.com/{org}/{slug}.git", str(dest)])
        return ("cloned" if rc2 == 0 else "clone-failed"), out[-200:]
    if not apply:
        return "would-create", f"https://github.com/{org}/{slug}.git"
    # create + seed a private repo
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "README.md").write_text(
        f"# {slug}\n\nPrivate fleet lane (D-128). Operational work for the "
        f"`{slug}` kanban board.\n")
    _run(["git", "init", "-q", "-b", "main"], cwd=dest)
    _run(["git", "-C", str(dest), "add", "-A"])
    _run(["git", "-C", str(dest), "commit", "-q", "-m",
          "chore: seed private lane repo (D-128)"])
    rc3, out = _run(["gh", "repo", "create", f"{org}/{slug}", "--private",
                     "--source", str(dest), "--push",
                     "--description", f"Private fleet lane repo ({slug})"])
    return ("created" if rc3 == 0 else "create-failed"), out[-200:]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-dir", default=str(DEFAULT_MAP))
    ap.add_argument("--org", default=DEFAULT_ORG)
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--only", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    md = Path(args.map_dir)

    private = _read(md / "private_boards.json", {}) or {}
    if not private:
        print("no private_boards.json (run the classifier first)")
        return 1
    boards = select_boards(private, args.limit, [x for x in args.only.split(",") if x])
    repos = _read(md / "repos.json", {}) or {}
    optin = _read(md / "private_offload_boards.json", {}) or {}

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(boards)} board(s)")
    for slug in boards:
        action, detail = ensure_repo(args.org, slug, args.apply)
        url = detail if detail.startswith("http") else f"https://github.com/{args.org}/{slug}.git"
        if (REPOS / slug / ".git").exists():
            url = _run(["git", "-C", str(REPOS / slug), "remote", "get-url", "origin"])[1] or url
        print(f"  {slug:32} {action:14} {url}")
        if args.apply and action in ("exists", "cloned", "created"):
            repos[slug] = url
            optin[slug] = {"repo": url, "private": True,
                           "ready": private[slug].get("ready", 0),
                           "todo": private[slug].get("todo", 0)}
    if args.apply:
        _write(md / "repos.json", repos)
        _write(md / "private_offload_boards.json", optin)
        print(f"registered {len(optin)} private offload board(s) in {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
