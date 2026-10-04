#!/usr/bin/env python3
"""worktree_quarantine.py — recurring sweep that quarantines orphaned worktrees.

This automates the 2026-09-20 disk remediation: unregistered / card-less
worktree dirs accumulate under ``~/worktrees`` and never get cleaned, because
the guarded reaper only reclaims *build artifacts* inside them. This MOVES the
whole orphan dir to the DQ05 external drive (a 1-year quarantine), verifies the
copy by sha256, and only then removes the local dir.

Safety:
  * KEEP if the dir is a registered git worktree (its gitdir still exists), has
    an open kanban card, is modified within ``idle_days``, or a live process has
    its cwd inside it.
  * dry-run by default; ``--apply`` performs the move.
  * the remote quarantine is skipped (nothing removed) if it is unreachable.
  * every action is logged to a ledger.

Usage:
  worktree_quarantine.py --policy state/fleet/worktree_quarantine.json [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

OPEN = {"ready", "running", "claimed", "blocked", "in_progress", "todo",
        "review", "triage"}
EXCLUDES = ["--exclude", "node_modules/", "--exclude", "target/",
            "--exclude", ".pio/", "--exclude", ".venv/",
            "--exclude", "__pycache__/", "--exclude", ".cache/"]


def open_cards() -> set:
    ids = set()
    boards = Path.home() / ".hermes" / "kanban" / "boards"
    for db in boards.glob("*/kanban.db"):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for tid, st in con.execute("SELECT id, status FROM tasks"):
                if st in OPEN:
                    ids.add(tid)
            con.close()
        except Exception:
            pass
    return ids


def active_cwd(wt_root: Path) -> set:
    out = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd.startswith(str(wt_root) + "/"):
                out.add(cwd[len(str(wt_root)) + 1:].split("/")[0])
        except Exception:
            pass
    return out


def is_registered(d: Path) -> bool:
    g = d / ".git"
    if g.is_dir():
        return True
    if g.is_file():
        try:
            line = g.read_text().strip()
            gitdir = line.split("gitdir:", 1)[1].strip()
            return Path(gitdir).exists()
        except Exception:
            return False
    return False


def candidates(wt_root: Path, idle_days: float):
    opens = open_cards()
    active = active_cwd(wt_root)
    out = []
    for d in sorted(wt_root.iterdir()):
        if not d.is_dir():
            continue
        tid = d.name.split("-")[0] if d.name.startswith("t_") else None
        if tid and tid in opens:
            continue
        if d.name in active or is_registered(d):
            continue
        try:
            idle = (time.time() - d.stat().st_mtime) / 86400.0
        except Exception:
            continue
        if idle < idle_days:
            continue
        out.append(d)
    return out


def remote_ok(remote: str) -> bool:
    host = remote.split(":", 1)[0]
    return subprocess.run(["ssh", "-o", "ConnectTimeout=8", host, "true"],
                          capture_output=True).returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    pol = json.loads(Path(args.policy).read_text())
    wt_root = Path(os.path.expanduser(pol.get("worktrees_dir", "~/worktrees")))
    idle_days = float(pol.get("idle_days", 7))
    remote = pol.get("remote", "")            # e.g. "dq05:/mnt/quarantine"
    qname = pol.get("quarantine_name", "worktrees")
    ledger = Path(os.path.expanduser(pol.get("ledger", "~/reports/worktree-quarantine.log")))
    ledger.parent.mkdir(parents=True, exist_ok=True)
    cands = candidates(wt_root, idle_days)
    stamp = time.strftime("%Y-%m-%d")
    print(f"[quarantine] {len(cands)} orphan candidate(s) (idle>{idle_days}d, no open card)")
    for d in cands:
        sz = subprocess.run(["du", "-sh", str(d)], capture_output=True, text=True).stdout.split()[0]
        print(f"  {'MOVE' if args.apply else 'WOULD MOVE'} {d.name} ({sz})")
    if not args.apply:
        return 0
    if not remote:
        print("[quarantine] no remote configured; nothing removed (would lose data)")
        return 0
    if not remote_ok(remote):
        print(f"[quarantine] remote {remote} unreachable; aborting (nothing removed)")
        return 0
    host, base = remote.split(":", 1)
    dest = f"{base.rstrip('/')}/{qname}-{stamp}"
    subprocess.run(["ssh", host, f"mkdir -p '{dest}'"], check=True)
    moved = kept = 0
    for d in cands:
        subprocess.run(["rsync", "-aHS", "-e", "ssh", *EXCLUDES,
                        f"{d}/", f"{host}:{dest}/{d.name}/"], capture_output=True)
        # verify: compare sha256 manifests
        if verify(d, host, f"{dest}/{d.name}"):
            shutil.rmtree(d, ignore_errors=True)
            moved += 1
            ledger.write_text(ledger.read_text() + f"{time.strftime('%F %T')} MOVED {d.name} -> {dest}\n") \
                if ledger.exists() else ledger.write_text(f"{time.strftime('%F %T')} MOVED {d.name} -> {dest}\n")
        else:
            kept += 1
            with open(ledger, "a") as lf:
                lf.write(f"{time.strftime('%F %T')} VERIFY-FAIL {d.name}\n")
    print(f"[quarantine] moved={moved} kept(verify-fail)={kept} -> {dest}")
    return 0


def _manifest(root: Path):
    import hashlib
    m = {}
    excl = {"node_modules", "target", ".pio", ".venv", "__pycache__", ".cache"}
    for dp, dn, fn in os.walk(root):
        dn[:] = [x for x in dn if x not in excl]
        for f in fn:
            p = Path(dp) / f
            try:
                rel = str(p.relative_to(root))
                m[rel] = "L:" + os.readlink(p) if p.is_symlink() else \
                    hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                m[str(p.relative_to(root))] = "ERR"
    return m


def verify(d: Path, host: str, dest: str) -> bool:
    loc = _manifest(d)
    script = (f"cd '{dest}' && find . -type f "
              "! -path '*/node_modules/*' ! -path '*/target/*' ! -path '*/.pio/*' "
              "! -path '*/.venv/*' ! -path '*/__pycache__/*' ! -path '*/.cache/*' "
              "-print0 | while IFS= read -r -d '' f; do rel=\"${f#./}\"; "
              "printf '%s\\t%s\\n' \"$rel\" \"$(sha256sum \"$f\" | cut -d' ' -f1)\"; done")
    out = subprocess.run(["ssh", host, "bash -s"], input=script,
                         capture_output=True, text=True, timeout=1800).stdout
    rem = dict(l.split("\t", 1) for l in out.splitlines() if "\t" in l)
    for rel, chk in loc.items():
        if rel not in rem:
            return False
        if not chk.startswith("L:") and rem[rel] != chk:
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
