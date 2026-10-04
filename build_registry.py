#!/usr/bin/env python3
"""build_registry.py — per-node repo registry with visibility (D-124/D-128).

Scans local git repos, resolves visibility (public/private) via `gh`, detects an
ngit remote, and writes ~/.hermes/bot/repo_registry.json:

  {"repos": {name: {path, default_branch, github, ngit, visibility,
                    mirror_issues}}}

Visibility classes drive push policy:
  public  -> GitHub + ngit
  private -> GitHub only
  local   -> nowhere (no remote)

Usage: build_registry.py [--scan DIR ...] [--out PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
OUT = HERMES / "bot" / "repo_registry.json"
DEFAULT_SCAN = [Path.home() / "repos", Path.home()]
EXTRA = [Path.home() / "hermes-orchestration", Path.home() / "kanban-boards",
         Path.home() / "reports"]

_GH = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


def _run(args, cwd=None, timeout=20) -> str:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception:
        return ""


def _git(repo: Path, *args) -> str:
    return _run(["git", "-C", str(repo)] + list(args))


def _visibility(url: str) -> str:
    if url.startswith("nostr://") or "relay.ngit.dev" in url:
        return "public"
    m = _GH.search(url)
    if not m:
        return "unknown"
    out = _run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}", "--jq", ".private"])
    if out == "false":
        return "public"
    if out == "true":
        return "private"
    return "unknown"


def _default_branch(repo: Path) -> str:
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/")[-1]
    for b in ("main", "master"):
        if _git(repo, "show-ref", "--verify", f"refs/heads/{b}"):
            return b
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "master"


def _remotes(repo: Path) -> dict:
    out = {}
    for line in _git(repo, "remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.setdefault(parts[0], parts[1])
    return out


def build(scan_dirs: list[Path]) -> dict:
    seen: set[Path] = set()
    repos = {}
    candidates: list[Path] = []
    for root in scan_dirs:
        if root.name == "repos" and root.is_dir():
            candidates += [p for p in root.iterdir() if (p / ".git").exists()]
        for extra in EXTRA:
            if extra.is_absolute() and (extra / ".git").exists():
                candidates.append(extra)
    for repo in candidates:
        rp = repo.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        remotes = _remotes(repo)
        origin = remotes.get("origin", "")
        ngit = remotes.get("ngit", "")
        if not origin and ngit:
            origin, ngit = ngit, ""
        vis = _visibility(origin) if origin else "local"
        if vis == "unknown":
            vis = "private"  # unknown GitHub visibility -> treat as private (safe)
        repos[repo.name] = {
            "path": str(repo), "default_branch": _default_branch(repo),
            "github": origin if "github" in origin else "",
            "ngit": ngit if ("nostr://" in ngit or "ngit" in ngit) else "",
            "visibility": "local" if not origin else vis,
            "mirror_issues": vis == "public",
        }
    return {"repos": repos}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="append", dest="scan")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    scan = [Path(s).expanduser() for s in (args.scan or [])] or DEFAULT_SCAN
    data = build(scan)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1))
    counts: dict[str, int] = {}
    for v in data["repos"].values():
        counts[v["visibility"]] = counts.get(v["visibility"], 0) + 1
    if args.json:
        print(json.dumps(data, indent=1))
    else:
        print(f"registry: {len(data['repos'])} repos -> {out} ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
