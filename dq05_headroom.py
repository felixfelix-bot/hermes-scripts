#!/usr/bin/env python3
"""dq05_headroom.py — probe DQ05's live resource headroom (zero tokens).

Foundation for the DQ05 offload lane (Phase F of
hermes-token-storm-remediation-plan.md). Samples DQ05 over SSH using the
existing `dq05` ssh-config alias and folds the raw metrics through the same
thresholds the local dispatch governor uses, writing
`~/.hermes/bot/dq05_headroom.json`.

This is deliberately read-only and fail-safe: if DQ05 cannot be reached the
file reports `can_dispatch=false` (never route work to a host we can't see).

Usage:  dq05_headroom.py [--print]
Cron:   */2 * * * * (see task-lifecycle / crontab)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
OUT = HOME / ".hermes" / "bot" / "dq05_headroom.json"

# Same raw hold thresholds as the local governor (kanban_watchers).
RAW_THRESHOLDS = {
    "cpu_load": 8.0,
    "memory_pct": 85.0,
    "swap_used_pct": 80.0,
    "disk_used_pct": 90.0,
}
# DQ05 is a 4-core / 11 GB box; allow up to 3 concurrent workers when idle.
STATIC_CAP = 3

_PROBE = (
    "nproc; "
    "cat /proc/loadavg; "
    "free -m | awk '/Mem:/{print $3\" \"$2}'; "
    "free -m | awk '/Swap:/{print $3\" \"$2}'; "
    "df -P / | tail -1 | awk '{print $5}'"
)


def _probe() -> dict:
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "dq05", _PROBE],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ssh failed").strip()[:200])
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) < 5:
        raise RuntimeError(f"unexpected probe output: {lines!r}")
    nproc = int(lines[0])
    load1 = float(lines[1].split()[0])
    mem_used, mem_total = (int(x) for x in lines[2].split())
    swap_used, swap_total = (int(x) for x in lines[3].split())
    disk_pct = float(lines[4].rstrip("%"))
    return {
        "nproc": nproc,
        "cpu_load": load1,
        "memory_pct": (100.0 * mem_used / mem_total) if mem_total else 0.0,
        "swap_used_pct": (100.0 * swap_used / swap_total) if swap_total else 0.0,
        "disk_used_pct": disk_pct,
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args()

    now = time.time()
    try:
        raw = _probe()
        per_dim = {}
        for k, thr in RAW_THRESHOLDS.items():
            v = raw.get(k, 0.0)
            per_dim[k] = 0.0 if v >= thr else (0.5 if v >= thr * 0.9 else 1.0)
        min_h = min(per_dim.values())
        target = 0 if min_h <= 0.0 else max(1, int(round(STATIC_CAP * min_h)))
        out = {
            "ts": now,
            "host": "dq05",
            "reachable": True,
            "raw": raw,
            "per_dimension": per_dim,
            "target_workers": target,
            "can_dispatch": target > 0,
            "reason": (
                "ok" if target > 0
                else "resource hold: " + ", ".join(
                    f"{k}={raw[k]:.1f} (>= {RAW_THRESHOLDS[k]})"
                    for k in RAW_THRESHOLDS if per_dim[k] <= 0.0
                )
            ),
        }
    except Exception as exc:  # fail-safe: unknown host => do not route there
        out = {
            "ts": now,
            "host": "dq05",
            "reachable": False,
            "target_workers": 0,
            "can_dispatch": False,
            "reason": f"probe_failed: {exc}",
        }

    try:
        OUT.write_text(json.dumps(out))
    except Exception:
        pass
    if args.do_print:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
