#!/usr/bin/env python3
"""kanban_mirror_tripwire.py — watchdog for the kanban board state-mirror repo.

The mirror (``~/hermes-kanban``) is a *state mirror*, not source: every sync
appends a commit of thousands of tiny JSON files, and each push duplicates the
reachable object set into a new pack. On 2026-10-06 it reached 2.5 GB / 18M
objects / 76 packs and starved the node. History of a state mirror has no value
— only the current tree matters — so the correct remediation is to squash it
back to ONE commit (``kanban_git.py compact``), not to gc it.

This watchdog reads the mirror's ``git count-objects -vH``, ``git rev-list
--count master``, and on-disk repo size, and alerts ONLY when a threshold is
exceeded. It prints nothing (and exits 0) when healthy, so it can run as a
no_agent cron where empty stdout means "no message sent". Each alert names the
metric, its value, the threshold, and the suggested action (run
``kanban_git.py compact``).

Usage:
  kanban_mirror_tripwire.py [--repo PATH]
      [--objects N] [--packs N] [--size-mb N] [--commits N]
      [--self-test]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DEFAULT_REPO = Path(os.environ.get("KANBAN_REPO", os.path.expanduser("~/hermes-kanban")))

# Sane defaults: a healthy mirror is ~1 commit / ~1 pack / a few MB.
DEFAULT_THRESHOLDS = {
    "objects": 200_000,   # git count-objects in-pack total
    "packs": 10,
    "size_mb": 200.0,
    "commits": 200,
}


def _gitdir(repo: Path) -> Path:
    if (repo / ".git").exists():
        return repo / ".git"
    if (repo / "HEAD").exists():
        return repo
    return repo / ".git"


def _repo_size_mb(repo: Path) -> float:
    gitdir = _gitdir(repo)
    total = 0
    for root, _dirs, files in os.walk(gitdir):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def collect_metrics(repo: Path) -> dict:
    """Measure the mirror's weight. Never raises; missing -> None metrics."""
    metrics: dict = {"objects": None, "packs": None, "commits": None, "size_mb": None}
    try:
        p = subprocess.run(["git", "-C", str(repo), "count-objects", "-vH"],
                           capture_output=True, text=True, timeout=60)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("in-pack:"):
                metrics["objects"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("packs:"):
                metrics["packs"] = int(line.split(":", 1)[1].strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "master"],
                           capture_output=True, text=True, timeout=60)
        metrics["commits"] = int((p.stdout or "").strip() or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        metrics["size_mb"] = round(_repo_size_mb(repo), 1)
    except Exception:  # noqa: BLE001
        pass
    return metrics


def evaluate(metrics: dict, thresholds: dict | None = None) -> list[dict]:
    """Return a finding for every metric past its threshold. Empty when healthy.

    ``thresholds`` keys are ``objects``/``packs``/``commits`` (ints) and
    ``size_mb`` (float). Each finding carries the metric name, value, threshold,
    and the suggested remediation action.
    """
    t = thresholds or DEFAULT_THRESHOLDS
    findings: list[dict] = []
    for key in ("objects", "packs", "commits", "size_mb"):
        value = metrics.get(key)
        limit = t.get(key)
        if value is None or limit is None:
            continue
        try:
            if float(value) > float(limit):
                findings.append({
                    "metric": key,
                    "value": value,
                    "threshold": limit,
                    "action": "run `kanban_git.py compact`",
                })
        except (TypeError, ValueError):
            continue
    return findings


def format_findings(findings: list[dict], repo: Path) -> str:
    lines = [f"kanban-mirror-tripwire: {repo} exceeded mirror-bloat thresholds"]
    for f in findings:
        lines.append(f"  - {f['metric']}: {f['value']} > {f['threshold']} "
                     f"-> {f['action']}")
    return "\n".join(lines)


def parse_thresholds_from_args(args: argparse.Namespace) -> dict:
    t = dict(DEFAULT_THRESHOLDS)
    if args.objects is not None:
        t["objects"] = args.objects
    if args.packs is not None:
        t["packs"] = args.packs
    if args.size_mb is not None:
        t["size_mb"] = args.size_mb
    if args.commits is not None:
        t["commits"] = args.commits
    return t


def self_test() -> int:
    """Exercise the threshold parser + decision logic; prints PASS/FAIL lines."""
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}"
              + (f"  :: {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # decision: under threshold -> no findings; over -> one finding per metric
    healthy = {"objects": 100, "packs": 1, "commits": 1, "size_mb": 1.0}
    check("healthy produces no findings", evaluate(healthy) == [])
    over = {"objects": 500_000, "packs": 12, "commits": 300, "size_mb": 250.0}
    fs = evaluate(over)
    check("four over-threshold metrics produce four findings", len(fs) == 4)
    check("finding names value+threshold+action",
          all("value" in f and "threshold" in f and "action" in f for f in fs))

    # boundary: exactly at threshold -> not exceeded (> is strict)
    at = {"objects": 200_000, "packs": 10, "commits": 200, "size_mb": 200.0}
    check("exactly-at-threshold is not exceeded", evaluate(at) == [])

    # missing metrics never alert
    check("missing metrics never alert", evaluate({}) == [])

    # parser: CLI overrides vs defaults
    ns = argparse.Namespace(objects=None, packs=None, size_mb=None, commits=None)
    check("parser defaults match DEFAULT_THRESHOLDS",
          parse_thresholds_from_args(ns) == DEFAULT_THRESHOLDS)
    ns2 = argparse.Namespace(objects=50, packs=3, size_mb=7.5, commits=9)
    t2 = parse_thresholds_from_args(ns2)
    check("parser honours CLI overrides",
          t2 == {"objects": 50, "packs": 3, "size_mb": 7.5, "commits": 9})

    print(f"\n{len(fails)}/{len(fails) + 1} self-test groups had failures"
          if False else f"\nself-test: {'FAIL' if fails else 'PASS'}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kanban mirror bloat watchdog")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--objects", type=int, default=None)
    ap.add_argument("--packs", type=int, default=None)
    ap.add_argument("--size-mb", type=float, default=None)
    ap.add_argument("--commits", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    repo = Path(os.path.expanduser(args.repo))
    thresholds = parse_thresholds_from_args(args)
    metrics = collect_metrics(repo)
    findings = evaluate(metrics, thresholds)

    # Silent + exit 0 when healthy: empty stdout means "no message sent".
    if not findings:
        return 0
    print(format_findings(findings, repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
