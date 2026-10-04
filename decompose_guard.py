#!/usr/bin/env python3
"""decompose_guard.py — gate kanban auto-decompose on capacity + daily caps.

The gateway auto-decomposer fans triage cards into sub-cards every tick. On
2026-09-15/16 that produced ~1000 idle cards because it ran while the
dispatcher had zero workers (nothing could drain the queue). This guard
re-enables `kanban.auto_decompose` ONLY when the fleet is actually executing
work and no board has exceeded its daily decomposition cap; otherwise it
disables it.

Idempotent, stdlib-only (cron/Ansible safe). Run every ~10 min.

Env:
  DECOMPOSE_GUARD_HEALTH_S       health window (default 900)
  DECOMPOSE_GUARD_MAX_PER_BOARD  per-board 24h cap (default 10)
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
CONFIGS = [HERMES / "config.yaml", HERMES / "profiles" / "manager" / "config.yaml"]
BOARDS = HERMES / "kanban" / "boards"
HEALTH_WINDOW_S = int(os.environ.get("DECOMPOSE_GUARD_HEALTH_S", "900"))
MAX_PER_BOARD_24H = int(os.environ.get("DECOMPOSE_GUARD_MAX_PER_BOARD", "10"))


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "select name from sqlite_master where type='table' and name=?", (name,)
    ).fetchone() is not None


def _recent_run() -> bool:
    """True if a task run started within the health window on any board."""
    now = time.time()
    for db in glob.glob(str(BOARDS / "*/kanban.db")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            if not _has_table(c, "task_runs"):
                c.close()
                continue
            cols = [r[1] for r in c.execute("pragma table_info(task_runs)")]
            tcol = next((x for x in ("started_at", "created_at", "ts") if x in cols), None)
            if not tcol:
                c.close()
                continue
            n = c.execute(
                f"select count(*) from task_runs where {tcol} > ?", (now - HEALTH_WINDOW_S,)
            ).fetchone()[0]
            c.close()
            if n > 0:
                return True
        except Exception:
            continue
    return False


def _dec_24h() -> dict:
    """auto-decomposer creations per board in the last 24h."""
    out: dict = {}
    now = time.time()
    for db in glob.glob(str(BOARDS / "*/kanban.db")):
        b = os.path.basename(os.path.dirname(db))
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            if not _has_table(c, "tasks"):
                c.close()
                continue
            n = c.execute(
                "select count(*) from tasks where created_by='auto-decomposer' "
                "and created_at > ?", (now - 86400,)
            ).fetchone()[0]
            c.close()
            out[b] = n
        except Exception:
            pass
    return out


def _set_configs(enabled: bool) -> int:
    """Flip kanban.auto_decompose in every config. Returns files changed."""
    want = f"auto_decompose: {'true' if enabled else 'false'}"
    changed = 0
    for f in CONFIGS:
        try:
            s = f.read_text()
            new = re.sub(r"(?m)^(\s*)auto_decompose:\s*(true|false)",
                         lambda m: f"{m.group(1)}{want}", s)
            if new != s:
                f.write_text(new)
                changed += 1
        except Exception:
            pass
    return changed


def main() -> int:
    dec = _dec_24h()
    over = {b: n for b, n in dec.items() if n >= MAX_PER_BOARD_24H}
    healthy = _recent_run()
    enable = healthy and not over
    changed = _set_configs(enable)
    print(f"decompose_guard: healthy={healthy} over_cap={over} "
          f"-> auto_decompose={'true' if enable else 'false'} "
          f"(configs_changed={changed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
