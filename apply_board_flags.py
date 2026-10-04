#!/usr/bin/env python3
"""apply_board_flags.py — apply canonical board flags from state/fleet/board_flags.json.

Config-as-code for board-level behaviour the dispatcher reads from ``board.json``
(e.g. ``no_llm_dispatch``: never spawn an LLM worker for this board — its work is
deterministic tooling, not LLM work). Idempotent.

Usage:
  apply_board_flags.py [--map PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS_ROOT = HERMES / "kanban" / "boards"
DEFAULT_MAP = Path(__file__).resolve().parent.parent.parent / "state" / "fleet" / "board_flags.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=str(DEFAULT_MAP))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mapping = json.loads(Path(args.map).read_text())
    if not isinstance(mapping, dict):
        print(f"bad map (not an object): {args.map}", file=sys.stderr)
        return 2

    changed = missing = 0
    for board, flags in sorted(mapping.items()):
        if not isinstance(flags, dict):
            continue
        bj = BOARDS_ROOT / board / "board.json"
        if not bj.exists():
            missing += 1
            continue
        try:
            cfg = json.loads(bj.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"skip {board}: {exc}", file=sys.stderr)
            continue
        if all(cfg.get(k) == v for k, v in flags.items()):
            continue
        cfg.update(flags)
        if not args.dry_run:
            bj.write_text(json.dumps(cfg, indent=2) + "\n")
        changed += 1
        print(f"{'[dry] ' if args.dry_run else ''}set {board} flags {flags}")
    print(f"applied={changed} missing_boards={missing} map={args.map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
