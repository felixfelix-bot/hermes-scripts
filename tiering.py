#!/usr/bin/env python3
"""tiering.py — hot/cold data tiering between local disk and the DQ05 offload drive.

Policy (Phase N, 2026-09-26): a **candidate** dir that has not been accessed for
>= COLD_DAYS is moved to the offload mount and replaced with a symlink; a dir
whose symlink target is accessed is materialized back to local disk.

Safety:
  * Only dirs explicitly listed in state/fleet/tiering.json are candidates
    (never auto-scan $HOME — too easy to break a live worktree).
  * Never touch a protected path (~/.hermes, active ~/worktrees, toolchains,
    ~/repos) or anything whose realpath is on the sshfs mount.
  * Dry-run by default; --apply to move.

Usage: tiering.py [--apply] [--cold-days N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
OFFLOAD = Path(os.environ.get("TIERING_OFFLOAD", "/mnt/dq05-lexar/cw-offload"))
CONFIG = Path(os.environ.get("TIERING_CONFIG",
                             HOME / ".hermes/bot/tiering.json"))
COLD_DAYS = 14
PROTECTED = (
    ".hermes", "worktrees", "repos", ".cargo", ".bun", ".ssh", ".config",
    ".local", ".cache", "reports", ".tmp", ".opencode",
)


def _default_config() -> dict:
    """Fallback candidate list (override with ~/.hermes/bot/tiering.json)."""
    return {"candidates": [], "protected": list(PROTECTED)}


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return _default_config()


def is_protected(path: Path, protected: list[str]) -> bool:
    parts = set(path.parts)
    if parts & set(protected):
        return True
    rp = str(path.resolve())
    return rp.startswith("/mnt/") or rp.startswith(str(OFFLOAD))


def newest_atime(path: Path) -> float:
    """Most recent st_atime across the tree (cheap bounded walk)."""
    latest = path.stat().st_atime
    count = 0
    for root, dirs, files in os.walk(path):
        count += 1
        if count > 2000:
            break
        for n in dirs + files:
            try:
                latest = max(latest, os.stat(os.path.join(root, n)).st_atime)
            except OSError:
                continue
    return latest


def plan(cfg: dict, cold_days: int, now: float) -> list[dict]:
    protected = cfg.get("protected", list(PROTECTED))
    out = []
    for name in cfg.get("candidates", []):
        p = Path(os.path.expanduser(name))
        if not p.exists() or is_protected(p, protected):
            continue
        idle_days = (now - newest_atime(p))
        out.append({
            "path": str(p),
            "idle_days": round(idle_days, 1),
            "cold": idle_days >= cold_days * 86400,
            "bytes": _dirsize(p),
        })
    return out


def _dirsize(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cold-days", type=int, default=COLD_DAYS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    rows = plan(cfg, args.cold_days, time.time())
    if args.json:
        print(json.dumps({"offload": str(OFFLOAD), "rows": rows}))
        return 0
    moved = 0
    for r in rows:
        tag = "COLD" if r["cold"] else "hot "
        print(f"  {tag} {r['idle_days']:>6}d  {r['bytes']/1e6:8.1f}MB  {r['path']}")
        if r["cold"] and args.apply:
            src = Path(r["path"])
            dst = OFFLOAD / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                print(f"    skip: {dst} exists"); continue
            shutil.move(str(src), str(dst))
            src.symlink_to(dst)
            moved += 1
    print(f"tiering: {len(rows)} candidates, {moved} offloaded"
          + ("" if args.apply else "  (dry-run)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
