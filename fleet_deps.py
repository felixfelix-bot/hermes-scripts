#!/usr/bin/env python3
"""fleet_deps.py — worker-dependency denylist for the fleet (D-128 8.15).

Enumerates processes that Hermes workers depend on and must NEVER be targeted by
any cleanup/reclaim. Covers the inference router, gateway, bridges, and any
LSP/build tooling under an active worktree.

Usage:
  fleet_deps.py list [--json]     # protected (pid, name, why)
  fleet_deps.py check <pid>       # exit 0 if protected
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Substrings that mark a worker-critical process (checked against cmdline).
DENY_CMDLINE = [
    "zai_proxy", "zai-proxy", "zai_proxy.py",
    "hermes-gateway", "hermes_cli.main gateway", "gateway run",
    "buzz-relay-bridge", "buzz-signal-bridge", "buzz_relay_bridge",
    "signal-cli", "model-selector", "tag-sidecar", "strfry",
    "git-remote-nostr", "ngit",
]
# Processes whose cwd lives under these are active-work tooling — never touch.
ACTIVE_DIR_MARKERS = ["/worktrees/", "/repos/"]


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", "ignore")
    except OSError:
        return ""


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _status(pid: int) -> str:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return line.split()[1]
    except OSError:
        pass
    return ""


def classify(cmd: str, cwd: str = "", state: str = "") -> str | None:
    """Pure: why is this process worker-critical? None if not protected."""
    for pat in DENY_CMDLINE:
        if pat in (cmd or ""):
            return f"denylist:{pat}"
    if cwd and any(m in cwd for m in ACTIVE_DIR_MARKERS) and state != "Z":
        return f"active-work:{cwd}"
    return None


def why_protected(pid: int) -> str | None:
    return classify(_cmdline(pid), _cwd(pid), _status(pid))


def protected_pids() -> dict[int, str]:
    out: dict[int, str] = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        why = why_protected(pid)
        if why:
            out[pid] = why
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("check"); p.add_argument("pid", type=int)
    args = ap.parse_args(argv)
    if args.cmd == "list":
        prot = protected_pids()
        if args.json:
            print(json.dumps(prot, indent=1))
        else:
            for pid, why in sorted(prot.items()):
                print(f"  {pid:>8} {why}")
            print(f"{len(prot)} protected process(es)")
        return 0
    if args.cmd == "check":
        why = why_protected(args.pid)
        if why:
            print(f"protected: {why}")
            return 0
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
