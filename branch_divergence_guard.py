#!/usr/bin/env python3
"""branch_divergence_guard.py — surface work that never got consolidated (L2).

Two failure classes this catches (both seen on 2026-09-16):
  * ORPHANED WIP: a worktree/repo with uncommitted changes, or a local branch
    carrying commits that are not in the default branch and have no upstream —
    work that "looks done" but was never landed (the genfix + live hermes-agent
    trees).
  * DRIFTED DEFAULT: the default branch is >N commits behind its upstream, so
    the fork is silently rotting.

Report-only + operator alert (never mutates). Exit 0 always.
~/.hermes/bot is deliberately absent: git-less deploy target by design (P6).
Usage: branch_divergence_guard.py [--repo PATH ...] [--max-behind N]
                                  [--max-age-days D] [--json] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_REPOS = [
    "~/hermes-orchestration",
    "~/.hermes/hermes-agent",
    "~/.hermes/profiles/manager/skills",
    "~/worktrees/router-503-fix",
]
MAX_BEHIND = int(os.environ.get("DIVERGENCE_MAX_BEHIND", "50"))
MAX_AGE_DAYS = float(os.environ.get("DIVERGENCE_MAX_AGE_DAYS", "3"))


def _git(repo: str, *args: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _default_branch(repo: str) -> str:
    for cand in ("origin/HEAD", "origin/main", "origin/master"):
        if _git(repo, "rev-parse", "--verify", "-q", cand):
            return cand
    return ""


def inspect_repo(repo: str, max_behind: int, max_age_days: float,
                 now: float | None = None) -> dict:
    """Return a dict describing divergence for *repo*. Never raises."""
    now = now if now is not None else time.time()
    out = {"repo": repo, "exists": os.path.isdir(os.path.join(repo, ".git"))
           or os.path.isdir(repo), "dirty": 0, "branches": [], "behind": None,
           "default": ""}
    if not out["exists"]:
        return out
    status = _git(repo, "status", "--porcelain")
    out["dirty"] = len([l for l in status.splitlines() if l.strip()])
    default = _default_branch(repo)
    out["default"] = default
    if not default:
        return out
    behind = _git(repo, "rev-list", "--count", f"HEAD..{default}")
    try:
        out["behind"] = int(behind)
    except ValueError:
        out["behind"] = None
    # Local branches with commits not in the default, older than max_age_days.
    refs = _git(repo, "for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)",
                "refs/heads")
    for line in refs.splitlines():
        try:
            name, ts = line.split("\t")
        except ValueError:
            continue
        if name in (default, "main", "master", "HEAD"):
            continue
        ahead = _git(repo, "rev-list", "--count", f"{default}..{name}")
        try:
            n = int(ahead)
        except ValueError:
            continue
        age_days = (now - float(ts or now)) / 86400.0
        if n > 0 and age_days >= max_age_days:
            out["branches"].append({"name": name, "ahead": n,
                                    "age_days": round(age_days, 1)})
    return out


def summarize(results: list[dict], max_behind: int) -> list[str]:
    issues = []
    for r in results:
        if not r.get("exists"):
            continue
        if r.get("dirty"):
            issues.append(f"{r['repo']}: {r['dirty']} uncommitted change(s)")
        for b in r.get("branches", []):
            issues.append(f"{r['repo']}: branch '{b['name']}' {b['ahead']} ahead, "
                          f"{b['age_days']}d old (not consolidated)")
        if r.get("behind") is not None and r["behind"] >= max_behind:
            issues.append(f"{r['repo']}: {r['default']} is {r['behind']} commits "
                          f"ahead of HEAD (drifted default)")
    return issues


def _alert(text: str) -> str:
    try:
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from operator_alert import post_alert  # type: ignore
        return post_alert(text, topic="branch-divergence", cooldown_s=86400)
    except Exception:
        return "unconfigured"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--max-behind", type=int, default=MAX_BEHIND)
    ap.add_argument("--max-age-days", type=float, default=MAX_AGE_DAYS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    repos = [os.path.expanduser(r) for r in (args.repo or DEFAULT_REPOS)]
    results = [inspect_repo(r, args.max_behind, args.max_age_days) for r in repos]
    issues = summarize(results, args.max_behind)
    if args.json:
        print(json.dumps({"issues": issues, "repos": results}, indent=1))
    else:
        print(f"branch-divergence-guard: {len(issues)} issue(s)")
        for i in issues:
            print("  -", i)
    if issues and not args.dry_run:
        _alert("⚠️ branch divergence:\n" + "\n".join(f"- {i}" for i in issues[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
