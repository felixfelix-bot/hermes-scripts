#!/usr/bin/env python3
"""mount_health_guard.py — alert on / self-heal the DQ05 offload sshfs mount.

Phase P5. A hung sshfs mount is the root of the fleet's silent-degradation
incidents (Go caches, reports, login shells). This guard runs `ls` under a hard
timeout so it can never itself block, and reports stalled/unmounted.

2026-09-27 durability fix: the offload is no longer purely cold-archival —
`~/.hermes/quarantine`, `~/.hermes/backups` and `graphify-out` symlink into it,
so an unmounted offload makes those dangling and silently breaks the reaper /
drift-check. With `--heal` the guard also *remounts* an unmounted (or stalled)
mount, using the fstab entry (`mount PATH`, falling back to `sudo -n mount`).

Usage: mount_health_guard.py [--strict] [--heal]
"""
from __future__ import annotations

import os
import subprocess
import sys

MOUNT = os.environ.get("OFFLOAD_MOUNT", "/mnt/dq05-lexar")
TIMEOUT = float(os.environ.get("MOUNT_HEALTH_TIMEOUT", "5"))


def check(path: str = MOUNT, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Return (stalled, reason). Never blocks longer than ~timeout+2 s.

    An unmounted offload mount is NOT an alert: since Phase N it is cold
    archival and nothing critical depends on it. Only a *mounted but hung*
    mount is a stall (that is what silently degrades Go caches etc.).
    """
    if not os.path.ismount(path):
        return False, "not mounted (offload is cold-archival)"
    try:
        r = subprocess.run(["timeout", str(int(timeout)), "ls", path],
                           capture_output=True, timeout=timeout + 2)
        if r.returncode != 0:
            return True, f"mounted but ls rc={r.returncode}"
        return False, f"ok ({timeout:.0f}s budget)"
    except subprocess.TimeoutExpired:
        return True, "mounted but stalled (stat timeout)"


def heal(path: str = MOUNT, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Attempt to (re)mount an unmounted offload. Returns (mounted, reason).

    Tries the fstab mount first, then passwordless sudo. Every attempt is
    hard-timeout bounded so a dead peer cannot hang the timer.
    """
    if os.path.ismount(path):
        return False, "already mounted"
    for cmd in (["mount", path], ["sudo", "-n", "mount", path]):
        try:
            subprocess.run(["timeout", str(int(timeout)), *cmd],
                           capture_output=True, timeout=timeout + 2)
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            continue
        if os.path.ismount(path):
            return True, f"remounted via {' '.join(cmd)}"
    return False, "remount failed (peer down or no privilege)"


def main(argv: list[str]) -> int:
    heal_mode = "--heal" in argv
    strict = "--strict" in argv
    stalled, why = check()
    if heal_mode and not os.path.ismount(MOUNT):
        did, hwhy = heal()
        print(f"mount-health: HEAL {MOUNT} {hwhy}")
        return 0 if (did or os.path.ismount(MOUNT)) else (1 if strict else 0)
    if stalled:
        print(f"mount-health: ALERT {MOUNT} {why}")
        return 1 if strict else 0
    print(f"mount-health: ok {MOUNT} ({why})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
