#!/usr/bin/env python3
"""pr_rereview_request.py — request re-review when a PR's head moved past the
SHA a human last reviewed (D-128 §15.5).

Problem: a PR approved / change-requested at an old SHA sits blocked with nobody
asking the reviewer to look at the new tip ("review starvation"). This script
finds those and re-requests the reviewers at the CURRENT head.

Idempotent: a reviewer already in `requested_reviewers` is left alone; only a
`CHANGES_REQUESTED`/`APPROVED` review whose `commit_id != head` triggers a
request. Dry-run by default; `--apply` performs the request.

Usage:
  pr_rereview_request.py [--repo OWNER/NAME]... [--apply] [--json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

OWN_AUTHORS = {"felixfelix-bot", "c03rad0r"}
REVIEW_DECISIONS = ("APPROVED", "CHANGES_REQUESTED")


def _gh(args: list[str]):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:  # noqa: BLE001
        return None
    return None


def rereview_targets(head_sha: str, reviews: list[dict], requested: set[str],
                     own_authors: set[str] = OWN_AUTHORS) -> list[str]:
    """Pure: logins whose latest substantive review predates the tip.

    `reviews` is the GitHub reviews list; the newest review per reviewer wins.
    """
    latest: dict[str, tuple[str, str]] = {}  # login -> (commit_id, state)
    for r in reviews:
        login = (r.get("user") or {}).get("login", "")
        state = (r.get("state") or "").upper()
        if not login or login in own_authors or state not in REVIEW_DECISIONS:
            continue
        latest[login] = (r.get("commit_id") or "", state)
    out = []
    for login, (commit, _state) in sorted(latest.items()):
        if commit and commit != head_sha and login not in requested:
            out.append(login)
    return out


def process_repo(repo: str, apply: bool) -> list[dict]:
    actions = []
    prs = _gh(["api", f"repos/{repo}/pulls?state=open&per_page=50"])
    if prs is None:
        print(f"[rereview] {repo}: API failure", file=sys.stderr)
        return actions
    for pr in prs:
        if (pr.get("user") or {}).get("login", "") not in OWN_AUTHORS:
            continue
        n = pr["number"]
        head = (pr.get("head") or {}).get("sha", "")
        reviews = _gh(["api", f"repos/{repo}/pulls/{n}/reviews?per_page=100"]) or []
        req = _gh(["api", f"repos/{repo}/pulls/{n}/requested_reviewers"]) or {}
        requested = {u.get("login", "") for u in req.get("users", [])}
        targets = rereview_targets(head, reviews, requested)
        if not targets:
            continue
        actions.append({"repo": repo, "pr": n, "head": head, "reviewers": targets})
        for login in targets:
            if apply:
                _gh(["api", "-X", "POST",
                     f"repos/{repo}/pulls/{n}/requested_reviewers",
                     "-f", f"reviewers[]={login}"])
    return actions


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    repos = args.repo or ["PlebeianApp/market"]
    all_actions = []
    for repo in repos:
        all_actions += process_repo(repo, args.apply)
    if args.json:
        print(json.dumps(all_actions, indent=1))
    else:
        verb = "requested" if args.apply else "would request"
        for a in all_actions:
            print(f"[rereview] {verb} {a['reviewers']} on {a['repo']}#{a['pr']} @ {a['head'][:8]}")
        if not all_actions:
            print("[rereview] nothing to re-request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
