#!/usr/bin/env python3
"""maintenance_window.py — schedule weekend-only maintenance (config-as-code).

Reads ``~/.hermes/bot/maintenance.json`` (windows + tasks). Runs hourly; inside a
window it **notifies the operator by default** and only **executes** a task whose
``enabled`` flag is true. This lets the operator schedule disruptive work
(provider-key rotation, Signal decommission) for a quiet weekend without
surprises.

Usage:
  maintenance_window.py            # notify-only: print what is due/scheduled
  maintenance_window.py --apply    # execute enabled tasks IF inside a window
  maintenance_window.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DEFAULT_CFG = HERMES / "bot" / "maintenance.json"

FALLBACK = {"version": 1,
            "windows": [{"days": ["Sat"], "start": "02:00", "end": "06:00",
                         "tz": "Europe/Berlin"}],
            "tasks": {}}
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CFG
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else dict(FALLBACK)
    except Exception:
        return dict(FALLBACK)


def _hm(s: str) -> int:
    h, m = (s or "0:0").split(":")
    return int(h) * 60 + int(m)


def in_window(now_epoch: float, windows: list[dict]) -> dict | None:
    """Return the active window (or None). Pure/testable. Handles wrap-midnight."""
    for w in windows or []:
        try:
            tz = ZoneInfo(w.get("tz") or "UTC")
        except Exception:
            tz = timezone.utc
        dt = datetime.fromtimestamp(now_epoch, tz=tz)
        days = w.get("days") or []
        day = _DAYS[dt.weekday()]
        prev_day = _DAYS[(dt.weekday() - 1) % 7]
        cur = dt.hour * 60 + dt.minute
        start, end = _hm(w.get("start")), _hm(w.get("end"))
        if start <= end:
            if start <= cur < end and (not days or day in days):
                return w
        else:  # wraps midnight
            if cur >= start and (not days or day in days):
                return w
            if cur < end and (not days or prev_day in days):
                return w
    return None


def due_tasks(cfg: dict) -> tuple[list[str], list[str]]:
    """(enabled, notify_only) task names."""
    enabled, notify = [], []
    for name, t in (cfg.get("tasks") or {}).items():
        if not isinstance(t, dict):
            continue
        if t.get("enabled"):
            enabled.append(name)
        else:
            notify.append(name)
    return enabled, notify


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    windows = cfg.get("windows") or []
    active = in_window(time.time(), windows)
    enabled, notify = due_tasks(cfg)

    if args.json:
        print(json.dumps({"in_window": bool(active), "window": active,
                          "enabled": enabled, "notify": notify}, indent=2))
        return 0

    if not active:
        print(f"maintenance: outside any window; scheduled={len(enabled)} "
              f"task(s) + {len(notify)} notify-only")
        return 0

    print(f"maintenance window ACTIVE: {active.get('days')} "
          f"{active.get('start')}-{active.get('end')} {active.get('tz')}")
    if not args.apply:
        for n in enabled + notify:
            msg = (cfg["tasks"].get(n) or {}).get("notify", "")
            print(f"  · {n}{' [enabled]' if n in enabled else ' [notify-only]'}"
                  + (f" — {msg}" if msg else ""))
        print("maintenance: notify-only (run --apply to execute enabled tasks)")
        return 0

    rc = 0
    for n in enabled:
        cmd = (cfg["tasks"].get(n) or {}).get("cmd", "")
        if not cmd:
            print(f"maintenance: {n} enabled but has no cmd; skipping")
            continue
        print(f"maintenance: running {n}: {cmd}")
        try:
            r = subprocess.run(cmd, shell=True, timeout=1800)
            rc = rc or r.returncode
        except Exception as e:
            print(f"maintenance: {n} failed: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
