#!/usr/bin/env python3
"""cron_expiry.py — disable cron jobs past their expiry (reversible).

Jobs pile up and keep doing work that is no longer required. This governor
disables (never deletes) an enabled job once it is past its expiry, so it stops
consuming slots/tokens until someone renews it.

Expiry sources (config-as-code, either):
  * the job's own ``expires_at`` field (ISO date/datetime), or
  * ``state/fleet/cron_expiries.json`` = {"<job name>": "<ISO date>"}

Renewal = remove the date / update it (or re-enable). Reversible.

Usage: cron_expiry.py [--apply] [--expiries PATH] [--now ISO]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import os
HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
JOBS = HERMES / "profiles" / "manager" / "cron" / "jobs.json"
DEFAULT_EXPIRIES = Path(__file__).resolve().parent.parent.parent / "state" / "fleet" / "cron_expiries.json"


def _parse(ts) -> float | None:
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def expired(jobs: list[dict], expiries: dict, now: float) -> list[str]:
    """Names of enabled jobs whose expiry is in the past."""
    out = []
    for j in jobs:
        name = j.get("name") or ""
        if not j.get("enabled", True):
            continue
        ts = _parse(j.get("expires_at")) or _parse(expiries.get(name))
        if ts is not None and ts < now:
            out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--expiries", default=str(DEFAULT_EXPIRIES))
    ap.add_argument("--jobs", default=str(JOBS))
    ap.add_argument("--now", default="")
    args = ap.parse_args()

    now = _parse(args.now) or time.time()
    try:
        expiries = json.loads(Path(args.expiries).read_text())
        if not isinstance(expiries, dict):
            expiries = {}
    except Exception:
        expiries = {}

    jp = Path(args.jobs)
    data = json.loads(jp.read_text())
    wrapper = data if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None
    jobs = data["jobs"] if wrapper is not None else data

    names = expired(jobs, expiries, now)
    if not names:
        print("cron-expiry: no expired jobs")
        return 0
    if not args.apply:
        print(f"cron-expiry: {len(names)} expired job(s) (report-only):")
        for n in names:
            print(f"  - {n}")
        return 0

    shutil.copy2(jp, jp.with_suffix(jp.suffix + f".bak-expiry-{time.strftime('%Y%m%d-%H%M%S')}"))
    n = 0
    for j in jobs:
        if (j.get("name") or "") in names and j.get("enabled", True):
            j["enabled"] = False
            j["paused"] = True
            j["paused_reason"] = "expired (cron_expiry); renew by clearing expires_at / re-enabling"
            n += 1
    if wrapper is not None:
        wrapper["jobs"] = jobs
        wrapper["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        jp.write_text(json.dumps(wrapper, indent=2) + "\n")
    else:
        jp.write_text(json.dumps(jobs, indent=2) + "\n")
    print(f"cron-expiry: disabled {n} expired job(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
