#!/usr/bin/env python3
"""disk_guard.py — free-space watermark guard for the worktree/toolchain disk.

Alerts (and optionally reclaims) when the root filesystem drops below a
watermark. This is the "don't wait for 93%" guardrail: the disk treadmill is
task-paced, so a fixed-percent alert fires too late. Config in
state/fleet/disk_guard.json.

When `prune_docker` is enabled the guard also reclaims Docker space: host
dangling volumes + builder cache on WARNING, and (on CRITICAL) unused images
plus a prune of the configured dind container (e.g. the ngit-ci dind) — the
dominant refill drivers on the CI node.

Exit is always 0 (a guard, not a gate). Emits a single alert line when low so
the no_agent cron layer surfaces it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULTS = {"path": "/", "warn_gb": 20, "critical_gb": 12,
            "reclaim_on_critical": True,
            "prune_docker": False,
            "prune_dind_container": ""}


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


def _run(cmd, timeout=1800):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 — a guard must never raise
        return f"skip ({exc})"


def _last(cmd, timeout=1800):
    lines = [ln for ln in _run(cmd, timeout).splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def docker_prune(cfg, aggressive: bool):
    """Reclaim Docker space. Conservative on WARNING, aggressive on CRITICAL."""
    if not cfg.get("prune_docker"):
        return []
    if not shutil.which("docker"):
        return ["docker: not installed"]
    out = ["host volume: " + _last(["docker", "volume", "prune", "-f"]),
           "host builder: " + _last(["docker", "builder", "prune", "-af" if aggressive else "-f"])]
    if aggressive:
        out.append("host images: " + _last(["docker", "image", "prune", "-a", "-f"]))
    dind = cfg.get("prune_dind_container") or ""
    if dind and _run(["docker", "inspect", "-f", "{{.State.Running}}", dind]).strip() == "true":
        out.append("dind: " + _last(["docker", "exec", dind, "docker", "system", "prune",
                                     "-af" if aggressive else "-f", "--volumes"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path.home() / ".hermes/bot/disk_guard.json"))
    args = ap.parse_args(argv)
    cfg = dict(DEFAULTS)
    p = Path(args.config)
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    gb = free_gb(cfg["path"])
    if gb < cfg["critical_gb"]:
        print(f"DISK CRITICAL: {gb:.1f}G free on {cfg['path']} "
              f"(< {cfg['critical_gb']}G)")
        for line in docker_prune(cfg, aggressive=True):
            print("prune:", line)
        if cfg.get("reclaim_on_critical"):
            r = subprocess.run(["python3", str(Path.home() / ".hermes/scripts/guarded_reaper.py"),
                                "--apply", "--include-blocked", "--blocked-stale-days", "30"],
                               capture_output=True, text=True, timeout=1800)
            print("reaper:", (r.stdout or r.stderr).strip().splitlines()[-1:] or "")
    elif gb < cfg["warn_gb"]:
        print(f"DISK WARNING: {gb:.1f}G free on {cfg['path']} (< {cfg['warn_gb']}G)")
        for line in docker_prune(cfg, aggressive=False):
            print("prune:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
