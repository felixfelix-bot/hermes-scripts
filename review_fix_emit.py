#!/usr/bin/env python3
"""review_fix_emit.py — turn a posted review's findings into scheduled fixes.

Reviews are posted automatically, but their fixes were not scheduled: the fix
half needs (a) an emitter from findings → cards, and (b) a split between
deterministic fixes (auto-dispatch) and design/security decisions (operator).

This reads a machine-readable findings file written by the reviewer alongside
the human review:

  ~/reports/reviews/<repo>-pr<N>-findings.json
  {
    "repo": "tidley/auditable-voting", "board": "auditable-voting", "pr": 28,
    "head": "a3c15ff7", "review_url": "https://github.com/.../pull/28#...",
    "tracks": [
      {"id": "A", "title": "Track A — windowed publication fixes",
       "branch": "pr/windowed-publication-fixes", "assignee": "worker-auditable-voting",
       "findings": [
         {"class": "deterministic", "severity": "block",
          "file": "web/src/x.ts:2244", "test": "...", "summary": "..."},
         {"class": "decision", "severity": "block", "summary": "...",
          "options": ["A: ...", "B: ..."]}
       ]}
    ]
  }

It creates ONE feature-level card per track (D-128 card economy; never one card
per finding), idempotent by repo#pr:track, and appends decision findings to the
operator decisions queue. Optionally publishes the review to the PR comments.

Usage:
  review_fix_emit.py --findings <file> [--apply] [--board B]
      [--publish-review --review-body <md>] [--gh-bin gh]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DECISIONS_QUEUE = Path.home() / ".hermes/state/review_decisions.jsonl"


def _render_body(f: dict, track: dict, repo: str, pr, head: str) -> str:
    lines = [f"Auto-emitted from the review of {repo} PR #{pr} @ {head}."]
    if f.get("review_url"):
        lines.append(f"Review: {f['review_url']}")
    if track.get("branch"):
        lines.append(f"Branch: `{track['branch']}` (one worker per branch, sequential).")
    lines.append("")
    lines.append("Deterministic findings (fix; TDD, run the local gates CI skips):")
    for i, fd in enumerate(f.get("findings", []), 1):
        loc = fd.get("file", "?")
        sev = fd.get("severity", "?")
        lines.append(f"{i}. [{sev}] {fd.get('summary','')}")
        lines.append(f"   - {loc}")
        if fd.get("test"):
            lines.append(f"   - proving test: {fd['test']}")
    return "\n".join(lines)


def plan_cards(findings: dict) -> tuple[list[dict], list[dict]]:
    """Pure: return (cards, decisions) from a findings manifest."""
    repo = findings.get("repo", "?")
    pr = findings.get("pr")
    head = findings.get("head", "?")
    board = findings.get("board")
    cards, decisions = [], []
    for track in findings.get("tracks", []):
        fs = track.get("findings", [])
        det = [f for f in fs if f.get("class") == "deterministic"]
        dec = [f for f in fs if f.get("class") == "decision"]
        if det:
            cards.append({
                "idempotency_key": f"{repo}#{pr}:{track['id']}",
                "board": board,
                "title": track["title"],
                "assignee": track.get("assignee"),
                "branch": track.get("branch"),
                "urgency": track.get("urgency", "soon"),
                "body": _render_body({"findings": det, "review_url": findings.get("review_url")},
                                     track, repo, pr, head),
            })
        for f in dec:
            decisions.append({
                "repo": repo, "pr": pr, "head": head, "track": track["id"],
                "board": board,
                "summary": f.get("summary", ""), "options": f.get("options", []),
                "review_url": findings.get("review_url"),
            })
    return cards, decisions


def _create_card(card: dict, gh: str, board_override: str | None, apply: bool) -> str:
    board = board_override or card.get("board")
    cmd = ["hermes", "kanban"]
    if board:
        cmd += ["--board", board]
    cmd += ["create", card["title"], "--urgency", card["urgency"],
            "--idempotency-key", card["idempotency_key"], "--body", card["body"]]
    if card.get("assignee"):
        cmd += ["--assignee", card["assignee"]]
    if card.get("branch"):
        cmd += ["--branch", card["branch"]]
    if not apply:
        return "DRY: " + " ".join(cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "?"


def _publish_review(repo: str, pr, body_file: str, apply: bool) -> str:
    cmd = ["gh", "pr", "comment", str(pr), "--repo", repo, "--body-file", body_file]
    if not apply:
        return "DRY: " + " ".join(cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return (r.stdout or r.stderr).strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", required=True)
    ap.add_argument("--board", default=None)
    ap.add_argument("--policy", default=str(Path.home() / ".hermes/bot/dispatch_granularity.json"))
    ap.add_argument("--publish-review", action="store_true")
    ap.add_argument("--review-body", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    findings = json.loads(Path(args.findings).read_text())
    cards, decisions = plan_cards(findings)

    # Granularity policy (config-as-code): apply defaults; the emitter already
    # produces exactly one feature-level card per track.
    pol = {}
    try:
        pol = json.loads(Path(args.policy).read_text())
    except Exception:
        pass
    for c in cards:
        c.setdefault("urgency", pol.get("default_urgency", "soon"))
        if not c.get("assignee") and pol.get("default_assignee"):
            c["assignee"] = pol["default_assignee"]
    for c in cards:
        print(f"[emit] card {c['idempotency_key']}: {_create_card(c, 'gh', args.board, args.apply)}")

    if decisions:
        if args.apply:
            DECISIONS_QUEUE.parent.mkdir(parents=True, exist_ok=True)
            with open(DECISIONS_QUEUE, "a") as fh:
                for d in decisions:
                    d["ts"] = int(time.time())
                    fh.write(json.dumps(d) + "\n")
            print(f"[emit] {len(decisions)} decision(s) queued -> {DECISIONS_QUEUE}")
        else:
            print(f"[emit] DRY: would queue {len(decisions)} decision(s) "
                  f"-> {DECISIONS_QUEUE}")

    if args.publish_review:
        if not args.review_body:
            print("[emit] --publish-review needs --review-body; skipped")
        else:
            print("[emit] review:", _publish_review(findings["repo"], findings["pr"],
                                                   args.review_body, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
