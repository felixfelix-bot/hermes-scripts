#!/usr/bin/env python3
"""cron_agent_guard.py — hard enforcement: agent cron jobs must be allowlisted.

Every cron job that runs an AGENT (``no_agent`` falsy) consumes tokens/memory
outside the kanban dispatcher's caps and headroom gate. Policy (locked
2026-09-17): only an explicit allowlist may run an agent directly; everything
else must be routed via the Kanban board (see cron_enqueue.py).

This guard **disables** (never deletes) non-conforming enabled jobs and records
why. Reversible: re-enable or add to the allowlist.

Config-as-code:
  allowlist <- state/fleet/cron_direct_allowlist.json (or --allowlist)
  jobs      <- ~/.hermes/profiles/manager/cron/jobs.json

Usage: cron_agent_guard.py [--apply] [--allowlist PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

# HERMES_HOME is a *profile* directory in agent/cron sessions (e.g.
# ~/.hermes/profiles/worker-plebeian), not the ~/.hermes root. Every path below used to be
# built from HERMES alone, so under a profile-scoped home they all drifted:
#
#   DEFAULT_ALLOWLIST = <file's dir>/../../state/fleet/cron_direct_allowlist.json
#       -> <root>/profiles/state/fleet/... which does not exist. A missing allowlist takes the
#          fail-closed branch, so the guard silently enforced nothing AND released nothing.
#   JOBS = $HERMES_HOME/profiles/manager/cron/jobs.json
#       -> <root>/profiles/worker-plebeian/profiles/manager/cron/jobs.json -> FileNotFoundError.
#
# Probe the env-provided home first, then the real ~/.hermes, and select the first candidate
# that exists (first one when none does, so the error names the intended path).
# Regression cover: tests/test_plebeian_review_enqueue.py::
#   test_guard_default_allowlist_points_at_the_live_file        (allowlist exists, under bot/)
#   test_guard_jobs_path_points_at_the_live_file                (jobs file exists)
#   test_guard_paths_survive_a_profile_scoped_hermes_home       (subprocess, foreign HERMES_HOME)
_ROOTS = tuple(dict.fromkeys((HERMES, Path(os.path.expanduser("~/.hermes")))))

_ALLOWLIST_CANDIDATES = tuple(r / "bot" / "cron_direct_allowlist.json" for r in _ROOTS)
DEFAULT_ALLOWLIST = next((p for p in _ALLOWLIST_CANDIDATES if p.exists()), _ALLOWLIST_CANDIDATES[0])

_JOBS_CANDIDATES = tuple(r / "profiles" / "manager" / "cron" / "jobs.json" for r in _ROOTS)
JOBS = next((p for p in _JOBS_CANDIDATES if p.exists()), _JOBS_CANDIDATES[0])


def load_jobs(path: Path):
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"], data
    return data, None


def nonconforming(jobs: list[dict], allow: set[str]) -> list[str]:
    """Enabled agent jobs (no_agent falsy) whose name is not allowlisted."""
    out = []
    for j in jobs:
        name = j.get("name") or ""
        if not j.get("enabled", True):
            continue
        if j.get("no_agent"):
            continue
        if name in allow:
            continue
        out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    ap.add_argument("--jobs", default=str(JOBS))
    args = ap.parse_args()

    allow_path = Path(args.allowlist)
    if not allow_path.exists():
        # Path drift here means the policy enforces nothing AND releases nothing while every
        # report still says "allowlisted". Name the path explicitly so drift is visible.
        print(f"cron-agent-guard: allowlist not found at {allow_path} "
              f"(live list should be {DEFAULT_ALLOWLIST}) — no action", file=sys.stderr)
        return 0

    try:
        allow_raw = json.loads(allow_path.read_text())
        allow = set(allow_raw.get("allow", []) if isinstance(allow_raw, dict) else allow_raw)
    except Exception as e:  # fail-closed: without an allowlist, change nothing
        print(f"cron-agent-guard: cannot read allowlist {allow_path} ({e}) — no action",
              file=sys.stderr)
        return 0

    jp = Path(args.jobs)
    if not jp.exists():
        # Same drift class as the allowlist above: name the path instead of tracebacking, so a
        # cron invocation that cannot find the job list is visible rather than a stack trace.
        print(f"cron-agent-guard: jobs file not found at {jp} "
              f"(default should resolve to {JOBS}) — no action", file=sys.stderr)
        return 0
    jobs, wrapper = load_jobs(jp)
    bad = nonconforming(jobs, allow)
    if not bad:
        print("cron-agent-guard: all enabled agent jobs are allowlisted")
        return 0

    if not args.apply:
        print(f"cron-agent-guard: {len(bad)} non-conforming agent job(s) (report-only):")
        for n in bad:
            print(f"  - {n}")
        return 0

    # Apply: disable, annotate, write back (with backup).
    shutil.copy2(jp, jp.with_suffix(jp.suffix + f".bak-agentguard-{time.strftime('%Y%m%d-%H%M%S')}"))
    n = 0
    for j in jobs:
        if (j.get("name") or "") in bad and j.get("enabled", True) and not j.get("no_agent"):
            j["enabled"] = False
            j["paused"] = True
            j["paused_reason"] = ("hard-enforced 2026-09-17: agent cron must be "
                                  "allowlisted or routed via kanban (cron_enqueue)")
            n += 1
    payload = wrapper if wrapper is not None else jobs
    if wrapper is not None:
        wrapper["jobs"] = jobs
        wrapper["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    jp.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"cron-agent-guard: disabled {n} non-conforming agent job(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
