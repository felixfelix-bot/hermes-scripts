#!/usr/bin/env python3
"""fleet_repo_sync.py — keep a node's offload repos cloned + updated (K.7).

Reads ~/.hermes/bot/fleet_repo_sync.json. Two forms:
  {"repos": {"market": "https://github.com/c03rad0r/market.git", ...},
   "update_existing": true}
  {"base_url": "https://github.com/c03rad0r", "repos": ["market", ...],
   "update_existing": true}

Clones any missing repo into ~/repos/<name> and (optionally) fetches existing
ones. Generalizes the offload lane across repos — no hard-coded list.

Usage: fleet_repo_sync.py [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
CFG = BOT / "fleet_repo_sync.json"
REPOS = Path.home() / "repos"


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _git(args, cwd=None, timeout=180) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = _read(CFG, {})
    repos_cfg = cfg.get("repos", [])
    base = cfg.get("base_url", "")
    update = bool(cfg.get("update_existing", True))
    # Normalize to a name -> url map.
    if isinstance(repos_cfg, dict):
        targets = dict(repos_cfg)
    elif isinstance(repos_cfg, list) and base:
        targets = {n: base.rstrip("/") + "/" + n + ".git" for n in repos_cfg}
    else:
        print("fleet_repo_sync: nothing configured (see ~/.hermes/bot/fleet_repo_sync.json)")
        return 0

    REPOS.mkdir(parents=True, exist_ok=True)
    results = []
    for name, url in targets.items():
        dst = REPOS / name
        if dst.exists():
            if not update:
                results.append({"repo": name, "action": "skip", "reason": "exists"})
                continue
            if args.dry_run:
                results.append({"repo": name, "action": "would-fetch"})
                continue
            rc, out = _git(["fetch", "--prune", "--quiet"], cwd=dst)
            results.append({"repo": name, "action": "fetch", "rc": rc,
                            "detail": out[-200:]})
        else:
            if args.dry_run:
                results.append({"repo": name, "action": "would-clone", "url": url})
                continue
            rc, out = _git(["clone", "--quiet", url, str(dst)])
            results.append({"repo": name, "action": "clone", "rc": rc,
                            "detail": out[-200:]})
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            extra = f"rc={r.get('rc')}" if "rc" in r else ""
            print(f"  {r['repo']:30} {r['action']:12} {extra} "
                  f"{str(r.get('detail',''))[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
