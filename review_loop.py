#!/usr/bin/env python3
"""review_loop.py — config-driven review->fix->re-review for arbitrary repos.

The Plebeian review pipeline (detector + gate + enqueue) is battle-tested but
hardcoded to `PlebeianApp/market`. This runner reads `review_targets.json` and
invokes those same scripts per target with `REVIEW_*` env overrides, so adding a
repo is a config change, not a code change.

Usage:
  review_loop.py list [--json]
  review_loop.py detect  [--repo NAME] [--dry-run] [--json]
  review_loop.py enqueue [--repo NAME] [--dry-run] [--json]

`--repo` may be repeated; omit it to run every enabled target. Exit code is 0
unless a target's underlying script hard-fails (non-zero, non-2).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
HERMES = HOME / ".hermes"
DEFAULT_REGISTRY = HERMES / "bot" / "review_targets.json"
DEFAULT_SCRIPTS_DIR = HERMES / "profiles" / "manager" / "scripts"
DEFAULT_STATE_DIR = HERMES / "profiles" / "manager" / "state"
DETECTOR = "plebeian-review-detector.py"
ENQUEUE = "plebeian-review-enqueue.py"


def load_targets(path: Path) -> dict:
    """Registry as {slug: resolved-target}; defaults merged in."""
    data = json.loads(Path(path).read_text())
    defaults = data.get("defaults", {})
    out = {}
    for name, cfg in (data.get("targets") or {}).items():
        merged = dict(defaults)
        merged.update(cfg)
        merged.setdefault("name", name)
        merged["slug"] = name
        out[name] = merged
    return out


def build_env(target: dict, mode: str, state_dir: Path | None = None,
              base_env: dict | None = None) -> dict:
    """Pure: the REVIEW_* environment for running the detector/enqueue."""
    if mode not in ("detect", "enqueue"):
        raise ValueError(f"unknown mode {mode!r}")
    state_dir = Path(state_dir or DEFAULT_STATE_DIR)
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    slug = target["slug"]
    env["REVIEW_REPO"] = target["github"]
    env["REVIEW_OWN_AUTHORS"] = ",".join(target.get("own_authors") or [])
    if mode == "detect":
        env["REVIEW_BOARD"] = target["review_board"]
        # Default "auto" (operator policy 2026-09-22, card t_7e34bae1): the
        # CARD CREATOR chooses — cheapest live cross-family reviewer, glm
        # first / author family excluded / kimi last — then gate 2 to a
        # DIFFERENT cross-family. A target-level worker-reviewer-* value is
        # an operator PIN and wins outright.
        env["REVIEW_ASSIGNEE"] = target.get("reviewer_assignee", "auto")
        env["REVIEW_GATE2_ASSIGNEE"] = target.get("gate2_assignee", "auto")
        env["REVIEW_PUBLISH"] = target.get("publish", "comment")
        env["PLEBEIAN_GATE_STATE"] = str(state_dir / f"review-{slug}-gate-state.json")
    elif mode == "enqueue":
        env["REVIEW_FIX_BOARD"] = target["fix_board"]
        env["REVIEW_FIX_ASSIGNEE"] = target.get("fix_assignee", "worker-plebeian")
        env["REVIEW_COVER_BOARDS"] = ",".join(
            b for b in (target.get("fix_board"), target.get("review_board")) if b)
        env["PLEBEIAN_REVIEW_ENQUEUE_STATE"] = str(
            state_dir / f"review-{slug}-enqueue-state.json")
        env["PLEBEIAN_REVIEW_ENQUEUE_LOG"] = str(
            state_dir / f"review-{slug}-enqueue.log")
        env["PLEBEIAN_REVIEW_ENQUEUE_QUOTA_LOG"] = str(
            state_dir / f"review-{slug}-enqueue-quota-skip.log")
    return env


def _script(name: str, scripts_dir: Path) -> Path | None:
    for base in (Path(scripts_dir), Path(__file__).resolve().parent,
                 Path(__file__).resolve().parent.parent / "manager" / "scripts"):
        p = base / name
        if p.exists():
            return p
    return None


def run_target(target: dict, mode: str, scripts_dir: Path, dry_run: bool,
               python: str | None = None) -> dict:
    script = _script(DETECTOR if mode == "detect" else ENQUEUE, scripts_dir)
    if script is None:
        return {"slug": target["slug"], "ok": False, "rc": 127,
                "out": f"script not found: {DETECTOR if mode == 'detect' else ENQUEUE}"}
    env = build_env(target, mode)
    cmd = [python or sys.executable, str(script)]
    if dry_run:
        cmd.append("--dry-run")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    # underlying scripts: 0 = ok/silent, 2 = hard fail (repo inaccessible/API down)
    return {"slug": target["slug"], "ok": p.returncode != 2, "rc": p.returncode,
            "out": out}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Config-driven review loop")
    ap.add_argument("mode", choices=("list", "detect", "enqueue"))
    ap.add_argument("--repo", action="append", default=None)
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--scripts-dir", default=str(DEFAULT_SCRIPTS_DIR))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    reg_path = Path(args.registry)
    if not reg_path.exists():
        repo_copy = Path(__file__).resolve().parent.parent.parent / "state" / "fleet" / "review_targets.json"
        reg_path = repo_copy if repo_copy.exists() else reg_path
    targets = load_targets(reg_path)

    if args.mode == "list":
        rows = [{"slug": t["slug"], "github": t["github"],
                 "review_board": t["review_board"], "fix_board": t["fix_board"],
                 "review_class": t.get("review_class"),
                 "enabled": t.get("enabled", True)} for t in targets.values()]
        print(json.dumps(rows, indent=1) if args.json
              else "\n".join(f"{r['slug']:30} {r['github']:45} "
                             f"review={r['review_board']} fix={r['fix_board']} "
                             f"{'on' if r['enabled'] else 'OFF'}" for r in rows))
        return 0

    selected = [t for t in targets.values()
                if t.get("enabled", True) and (not args.repo or t["slug"] in args.repo)]
    if args.repo:
        missing = [r for r in args.repo if r not in targets]
        if missing:
            print(f"unknown target(s): {', '.join(missing)}", file=sys.stderr)
            return 2
    results = [run_target(t, args.mode, Path(args.scripts_dir), args.dry_run)
               for t in selected]
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"[{args.mode}] {r['slug']}: rc={r['rc']} {'OK' if r['ok'] else 'FAIL'}")
            if r["out"]:
                print("  " + r["out"].replace("\n", "\n  "))
    return 1 if any(not r["ok"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
