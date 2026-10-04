#!/usr/bin/env python3
"""apply_board_repos.py — apply the canonical board->repo map to live board.json.

Reads ``state/fleet/board_repos.json`` (canonical, version-controlled) and writes
``repo`` into each board's ``board.json`` idempotently. This makes the offload
fit resolution reproducible via Ansible instead of relying on live edits plus a
daily state snapshot.

Usage:
  apply_board_repos.py [--map PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS_ROOT = HERMES / "kanban" / "boards"
DEFAULT_MAP = Path(__file__).resolve().parent.parent.parent / "state" / "fleet" / "board_repos.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=str(DEFAULT_MAP))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mapping = json.loads(Path(args.map).read_text())
    if not isinstance(mapping, dict):
        print(f"bad map (not an object): {args.map}", file=sys.stderr)
        return 2

    changed = 0
    missing_board = 0
    for board, repo in sorted(mapping.items()):
        bj = BOARDS_ROOT / board / "board.json"
        if not bj.exists():
            missing_board += 1
            continue
        try:
            cfg = json.loads(bj.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"skip {board}: {exc}", file=sys.stderr)
            continue
        if str(cfg.get("repo") or "").strip() == str(repo).strip():
            continue
        cfg["repo"] = repo
        if not args.dry_run:
            bj.write_text(json.dumps(cfg, indent=2) + "\n")
        changed += 1
        print(f"{'[dry] ' if args.dry_run else ''}set {board} -> {repo}")
    print(f"applied={changed} missing_boards={missing_board} map={args.map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
