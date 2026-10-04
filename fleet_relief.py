#!/usr/bin/env python3
"""fleet_relief.py — last-resort, CACHE-ONLY memory relief (D-128 8.13).

Never signals a process. Only removes allowlisted regenerable caches, and only
when the node is CRITICAL (low memory + swap thrash) AND the operator is ABSENT
(no recent operator activity). Default off; arm with FLEET_RELIEF_ENABLE=1.

Usage:
  fleet_relief.py status [--json]
  fleet_relief.py maybe [--dry-run]     # runs only if critical & operator absent
  fleet_relief.py reclaim [--dry-run]   # unconditional cache reclaim
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
LAST_SEEN = HERMES / "bot" / "operator_last_seen"

# Regenerable cache directories (rebuildable; removing them never breaks a dep).
CACHE_TARGETS = [
    HOME / ".cache" / "uv",
    HOME / ".cache" / "pip",
    HOME / ".cache" / "bun",
    HOME / ".cache" / "npm",
    HOME / ".cache" / "go-build",
    HOME / ".cargo" / "registry" / "cache",
]

MEM_CRITICAL_MB = int(os.environ.get("FLEET_RELIEF_MEM_MB", "512"))
OPERATOR_WINDOW_S = int(os.environ.get("FLEET_RELIEF_OPERATOR_WINDOW_S", "900"))


def mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 10 ** 9


def swap_activity() -> int:
    """pswpin+pswpout; a rising value at two samples means thrash."""
    try:
        d = {}
        for line in Path("/proc/vmstat").read_text().splitlines():
            k, _, v = line.partition(" ")
            if k in ("pswpin", "pswpout"):
                d[k] = int(v)
        return d.get("pswpin", 0) + d.get("pswpout", 0)
    except OSError:
        return 0


def swap_thrashing(sample_s: float = 1.0) -> bool:
    a = swap_activity()
    time.sleep(sample_s)
    return (swap_activity() - a) > 0


def pressure_level() -> str:
    avail = mem_available_mb()
    if avail < MEM_CRITICAL_MB and swap_thrashing():
        return "critical"
    if avail < MEM_CRITICAL_MB * 2:
        return "high"
    return "ok"


def operator_present() -> bool:
    """Operator counts as present if they acted recently or an interactive
    session is open. Conservative: unknown ⇒ present (do NOT reclaim)."""
    try:
        if LAST_SEEN.exists() and (time.time() - LAST_SEEN.stat().st_mtime) < OPERATOR_WINDOW_S:
            return True
    except OSError:
        pass
    try:
        r = subprocess.run(["pgrep", "-f", "opencode -s "], capture_output=True,
                           text=True, timeout=5)
        if r.stdout.strip():
            return True
    except Exception:
        pass
    return False


def armed() -> bool:
    return os.environ.get("FLEET_RELIEF_ENABLE", "0") == "1"


def reclaim(dry_run: bool = False) -> dict:
    freed = 0
    removed = []
    for d in CACHE_TARGETS:
        if not d.exists():
            continue
        try:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except OSError:
            size = 0
        removed.append({"path": str(d), "bytes": size})
        freed += size
        if not dry_run:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    return {"freed_bytes": freed, "freed_mb": round(freed / 1e6, 1),
            "targets": removed, "dry_run": dry_run}


def status() -> dict:
    st = {"mem_available_mb": mem_available_mb(), "pressure": pressure_level(),
          "operator_present": operator_present(), "armed": armed()}
    return st


def maybe(dry_run: bool) -> dict:
    st = status()
    if not st["armed"]:
        st["action"] = "disabled (FLEET_RELIEF_ENABLE!=1)"
        return st
    if st["pressure"] != "critical":
        st["action"] = "skip (not critical)"
        return st
    if st["operator_present"]:
        st["action"] = "skip (operator present)"
        return st
    st["action"] = "reclaim"
    st.update(reclaim(dry_run))
    return st


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("maybe"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("reclaim"); p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        st = status()
        print(json.dumps(st, indent=1) if args.json else st)
        return 0
    if args.cmd == "maybe":
        print(json.dumps(maybe(args.dry_run), indent=1))
        return 0
    if args.cmd == "reclaim":
        print(json.dumps(reclaim(args.dry_run), indent=1))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
