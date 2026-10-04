#!/usr/bin/env python3
"""ensure_cron.py — idempotent cron convergence (D-132 addendum).

`hermes cron create` appends a new job on every playbook run (the source of the
duplicate gate-tick/completion-watch/etc. jobs). This helper converges the store
to exactly one job per name:

  * 0 matching  -> create one
  * 1 matching  -> leave it
  * >1 matching -> keep the best (schedule match, last run ok) and remove the
                   rest **by ID** (name-based removal raises
                   AmbiguousJobReference)

It mutates only through the `hermes` CLI, and targets the store selected by
``--hermes-home`` (a profile dir is honoured). Exit 0 on success.

Usage:
  ensure_cron.py --name N --script S --schedule "*/30 * * * *" \
      [--deliver local] [--hermes-home PATH] [--hermes-bin PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def home_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~/.hermes"))


def load_jobs(home: Path) -> list:
    f = home / "cron" / "jobs.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return []
    jobs = d if isinstance(d, list) else d.get("jobs", [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return jobs or []


def sched_of(job: dict) -> str:
    s = job.get("schedule") or {}
    return s.get("display") or s.get("expr") or str(s.get("minutes") or "")


def _stable_offset(name: str, modulo: int) -> int:
    if modulo <= 1:
        return 0
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return int(h, 16) % modulo


def jitter_schedule(schedule: str, name: str, window: int = 5) -> str:
    """Deterministically offset a 5-field cron so jobs don't all fire at :00.

    ADR-003/004: dozens of fleet jobs were registered at ``*/N`` and ``:00``,
    producing a thundering-herd at the top of every hour/step. This derives a
    STABLE per-name offset (same name -> same schedule, so the converge stays
    idempotent):

      * ``*/N * * * *`` -> ``M/N * * * *`` with M = hash(name) % N
      * fixed minute M  -> minute shifted by hash(name) % window (mod 60)

    Non-numeric/non-step minutes (ranges, lists) are returned unchanged.
    """
    if int(window) <= 0:
        return schedule
    parts = schedule.split()
    if len(parts) != 5:
        return schedule
    minute = parts[0]
    if minute.startswith("*/"):
        try:
            n = int(minute[2:])
        except ValueError:
            return schedule
        if n > 1:
            parts[0] = f"{_stable_offset(name, n)}/{n}"
        return " ".join(parts)
    if minute.isdigit():
        m = int(minute)
        w = max(1, int(window))
        parts[0] = str((m + _stable_offset(name, w)) % 60)
        return " ".join(parts)
    return schedule


def run(hermes_bin: str, home: Path, *args: str) -> int:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    proc = subprocess.run([hermes_bin, *args], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--script", required=True)
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--deliver", default="local")
    ap.add_argument("--workdir", default=None,
                    help="absolute workdir for the job (passed to cron create)")
    ap.add_argument("--create-if-missing", action="store_true",
                    help="create the job when none exists (default: dedupe only)")
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--hermes-bin", default="hermes")
    ap.add_argument("--jitter", type=int, default=0,
                    help="spread jobs off :00/:step; window in minutes for a "
                         "fixed-minute schedule (0 = off, default). Stable per "
                         "--name so the converge stays idempotent.")
    args = ap.parse_args(argv)

    schedule = (jitter_schedule(args.schedule, args.name, args.jitter)
                if args.jitter > 0 else args.schedule)
    home = home_dir(args.hermes_home)
    jobs = load_jobs(home)
    matches = [j for j in jobs if (j.get("name") or "") == args.name]

    def score(j):
        return (0 if sched_of(j) == schedule else 1,
                0 if j.get("last_status") == "ok" else 1)

    actions = []
    ok = True
    if len(matches) > 1:
        keeper = sorted(matches, key=score)[0]
        for j in matches:
            if j.get("id") != keeper.get("id"):
                rc = run(args.hermes_bin, home, "cron", "remove", j["id"])
                actions.append(f"removed:{j['id']}" if rc == 0 else f"remove-failed:{j['id']}")
                ok = ok and rc == 0
        matches = [keeper]

    if not matches:
        if not args.create_if_missing:
            actions.append("absent")
        else:
            create_args = ["cron", "create", "--no-agent",
                           "--script", args.script, "--name", args.name,
                           "--deliver", args.deliver]
            if args.workdir:
                create_args += ["--workdir", args.workdir]
            create_args.append(schedule)
            rc = run(args.hermes_bin, home, *create_args)
            actions.append("created" if rc == 0 else f"create-failed:{rc}")
            ok = ok and rc == 0
    else:
        actions.append("present")

    print(f"{args.name}: {', '.join(actions)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
