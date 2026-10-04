#!/usr/bin/env python3
"""fleet_lkg.py — last-known-good (LKG) snapshot store for the Hermes fleet.

Snapshots *behavioral* config + scripts + cron job stores, hashes them, and
can restore an atomic, verified rollback point. This is the rollback backend
for ``fleet_remediate.py L7``.

Deliberately EXCLUDES, so a rollback can never clobber the remediator or leak
credentials:
  * secrets: ``.env``, ``*.nsec``, ``*.npub``
  * the fleet tooling itself: ``fleet_*.py`` / ``fleet_*.sh``
  * freeze sentinels: ``ESTOP``, ``.dispatch_frozen``, ``.fleet_quarantine``
  * backups: ``*.bak*`` and ``__pycache__``
  * symlinks (they point at repo files that are version-controlled separately)

Pure stdlib. Profile-aware via ``HERMES_HOME`` (default ``~/.hermes``).

Usage:
  fleet_lkg.py snapshot [--label TEXT]
  fleet_lkg.py list [--json]
  fleet_lkg.py mark-good <id> [--note TEXT]
  fleet_lkg.py verify <id>            # drift report vs current filesystem
  fleet_lkg.py restore <id> [--dry-run] [--yes]
  fleet_lkg.py prune --keep N
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
ROOT = Path(os.environ.get("FLEET_LKG_ROOT", str(HERMES_HOME / "bot" / "fleet_lkg")))
INDEX = ROOT / "index.json"
SCHEMA = 1

_EXCLUDE_SUBSTR = (".bak", "__pycache__", ".dispatch_frozen", ".fleet_quarantine")
_EXCLUDE_SUFFIX = (".nsec", ".npub", ".env")
_EXCLUDE_NAMES = {"ESTOP", "index.json"}

# Router/engine state worth snapshotting alongside code (D-133). Matched by
# substring against the file NAME in ~/.hermes/bot/. DBs are excluded by
# extension here; learned Kalman data is exported to state/router/ separately.
_ROUTER_STATE_SUBSTR = (
    "kalman", "compression", "zai_proxy_state", "pool_kalman", "ppq_usage",
    "historical_btc", "live_catalog", "router", "flat", "lane", "proxy",
)


def _excluded(p: Path) -> bool:
    name = p.name
    if name in _EXCLUDE_NAMES:
        return True
    if name.startswith("fleet_"):
        return True
    if name.endswith(_EXCLUDE_SUFFIX) or name == ".env":
        return True
    if any(s in name for s in _EXCLUDE_SUBSTR):
        return True
    if p.is_symlink():
        return True
    return False


def _targets() -> Iterable[Path]:
    """Yield candidate files to snapshot (relative to HERMES_HOME)."""
    direct = [
        HERMES_HOME / "config.yaml",
        HERMES_HOME / "cron" / "jobs.json",
    ]
    for p in direct:
        if p.is_file() and not _excluded(p):
            yield p

    scripts = HERMES_HOME / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.iterdir()):
            if p.is_file() and p.suffix in (".sh", ".py") and not _excluded(p):
                yield p

    # Live router + engine code and learned router state (D-133): the flat
    # router proxy and its Kalman/compression state live in ~/.hermes/bot/,
    # which was previously NOT snapshotted — the exact gap that let a stale
    # deploy clobber the live router with no rollback point.
    bot = HERMES_HOME / "bot"
    if bot.is_dir():
        for p in sorted(bot.iterdir()):
            if not p.is_file() or _excluded(p):
                continue
            if p.suffix in (".sh", ".py"):
                yield p
            elif p.suffix == ".json" and any(s in p.name for s in _ROUTER_STATE_SUBSTR):
                yield p

    profiles = HERMES_HOME / "profiles"
    if profiles.is_dir():
        for prof in sorted(profiles.iterdir()):
            if not prof.is_dir():
                continue
            for rel in ("config.yaml", "cron/jobs.json"):
                p = prof / rel
                if p.is_file() and not _excluded(p):
                    yield p
            pdir = prof / "scripts"
            if pdir.is_dir():
                for p in sorted(pdir.iterdir()):
                    if p.is_file() and p.suffix in (".sh", ".py") and not _excluded(p):
                        yield p


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default if default is not None else {}


def _load_index() -> dict:
    try:
        data = json.loads(INDEX.read_text())
        if isinstance(data, dict) and "snapshots" in data:
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "snapshots": []}


def _save_index(idx: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, indent=1))
    tmp.replace(INDEX)


def _rel(p: Path) -> str:
    return str(p.relative_to(HERMES_HOME))


def _new_id() -> str:
    """Unique snapshot id; never collides with an existing snapshot dir.

    Second-resolution alone is not enough: a restore takes a pre-restore
    safety snapshot in the same second as the snapshot it is restoring, which
    previously overwrote that snapshot's payload with the drifted files.
    """
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid = base
    n = 0
    while (ROOT / sid).exists() or _find(_load_index(), sid) is not None:
        n += 1
        sid = f"{base}-{n}"
    return sid


def cmd_snapshot(args) -> int:
    sid = _new_id()
    dest = ROOT / sid
    files: dict[str, str] = {}
    for p in _targets():
        rel = _rel(p)
        out = dest / "files" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        files[rel] = _sha256(p)
    meta = {
        "id": sid,
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "label": args.label or "",
        "status": "candidate",
        "note": "",
        "files": files,
        "count": len(files),
    }
    idx = _load_index()
    idx["snapshots"] = [s for s in idx["snapshots"] if s.get("id") != sid]
    idx["snapshots"].append(meta)
    _save_index(idx)
    print(f"snapshot {sid}: {len(files)} files -> {dest}")
    return 0


def cmd_list(args) -> int:
    idx = _load_index()
    if args.json:
        print(json.dumps(idx, indent=1))
        return 0
    for s in sorted(idx["snapshots"], key=lambda x: x.get("ts", 0), reverse=True):
        mark = {"good": "*", "candidate": "-", "rolled-back": "!"}.get(s.get("status"), "?")
        print(f"{mark} {s['id']}  {s.get('status'):12} files={s.get('count'):3} "
              f"{s.get('label','')} {s.get('note','')}")
    return 0


def _find(idx: dict, sid: str) -> dict | None:
    for s in idx["snapshots"]:
        if s.get("id") == sid:
            return s
    return None


def cmd_mark_good(args) -> int:
    idx = _load_index()
    s = _find(idx, args.id)
    if s is None:
        print(f"no such snapshot: {args.id}", file=sys.stderr)
        return 1
    s["status"] = "good"
    s["note"] = args.note or s.get("note", "")
    s["verified_at"] = time.time()
    _save_index(idx)
    print(f"{args.id} marked good")
    return 0


def cmd_verify(args) -> int:
    idx = _load_index()
    s = _find(idx, args.id)
    if s is None:
        print(f"no such snapshot: {args.id}", file=sys.stderr)
        return 1
    drift = []
    for rel, want in s.get("files", {}).items():
        cur = HERMES_HOME / rel
        if not cur.exists():
            drift.append(f"MISSING  {rel}")
        elif _sha256(cur) != want:
            drift.append(f"CHANGED  {rel}")
    if drift:
        print(f"{args.id}: DRIFT ({len(drift)})")
        for d in drift:
            print("  " + d)
        return 1
    print(f"{args.id}: clean ({len(s.get('files', {}))} files)")
    return 0


def cmd_restore(args) -> int:
    idx = _load_index()
    s = _find(idx, args.id)
    if s is None:
        print(f"no such snapshot: {args.id}", file=sys.stderr)
        return 1
    src_dir = ROOT / args.id / "files"
    if not src_dir.is_dir():
        print(f"snapshot payload missing: {src_dir}", file=sys.stderr)
        return 1
    plan = []
    for rel in s.get("files", {}):
        src = src_dir / rel
        dst = HERMES_HOME / rel
        if not src.is_file():
            plan.append(("SKIP", rel))
            continue
        if dst.exists() and dst.is_file() and _sha256(dst) == _sha256(src):
            plan.append(("SAME", rel))
        else:
            plan.append(("RESTORE", rel))
    to_do = [r for a, r in plan if a == "RESTORE"]
    if args.dry_run:
        for action, rel in plan:
            print(f"  {action:7} {rel}")
        print(f"dry-run: {len(to_do)} file(s) would be restored from {args.id}")
        return 0
    if not args.yes:
        print(f"refusing to restore {len(to_do)} file(s) without --yes "
              f"(use --dry-run to preview)", file=sys.stderr)
        return 1
    # Take a safety snapshot of current state first.
    cmd_snapshot(argparse.Namespace(label=f"pre-restore-of-{args.id}"))
    restored = 0
    for rel in to_do:
        src = src_dir / rel
        dst = HERMES_HOME / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".lkg-tmp")
        shutil.copy2(src, tmp)
        tmp.replace(dst)
        restored += 1
    # Reload: the safety snapshot above rewrote the index.
    idx = _load_index()
    s2 = _find(idx, args.id)
    if s2 is not None:
        s2["status"] = "rolled-back"
        _save_index(idx)
    print(f"restored {restored} file(s) from {args.id}")
    return 0


def cmd_maybe_good(args) -> int:
    """Promote the latest candidate snapshot to 'good' once the node has had a
    sustained clean window (read from fleet_arbiter_state.json). Idempotent.
    """
    import socket
    cfg = _read_json(HERMES_HOME / "bot" / "fleet.json", {})
    me = cfg.get("node") or socket.gethostname()
    arb = _read_json(HERMES_HOME / "bot" / "fleet_arbiter_state.json", {})
    clean = int((arb.get(me, {}) or {}).get("clean_streak", 0) or 0)
    if clean < args.clean_ticks:
        print(f"maybe-good: clean_streak={clean} < {args.clean_ticks}; skip")
        return 0
    idx = _load_index()
    snaps = sorted(idx["snapshots"], key=lambda s: s.get("ts", 0), reverse=True)
    cand = next((s for s in snaps if s.get("status") == "candidate"), None)
    if not cand:
        print("maybe-good: no candidate snapshot")
        return 0
    good = max((s.get("ts", 0) for s in snaps if s.get("status") == "good"),
               default=0)
    if cand.get("ts", 0) <= good:
        print("maybe-good: candidate not newer than latest good; skip")
        return 0
    cand["status"] = "good"
    cand["note"] = (cand.get("note", "") + " auto-good(clean window)").strip()
    cand["verified_at"] = time.time()
    _save_index(idx)
    print(f"maybe-good: promoted {cand['id']} to good (clean_streak={clean})")
    return 0


def cmd_prune(args) -> int:
    """Keep the newest `--keep` snapshots AND every 'good' rollback point.

    'good' snapshots are verified rollback targets; age must never evict them
    (a 2026-09 bug pruned two auto-good snapshots because only recency was
    considered).
    """
    idx = _load_index()
    snaps = sorted(idx["snapshots"], key=lambda x: x.get("ts", 0), reverse=True)
    protected = {s["id"] for s in snaps if s.get("status") == "good"}
    keep, drop = [], []
    for i, s in enumerate(snaps):
        if i < max(0, args.keep) or s["id"] in protected:
            keep.append(s)
        else:
            drop.append(s)
    for s in drop:
        shutil.rmtree(ROOT / s["id"], ignore_errors=True)

    # Orphan sweep: directories present on disk but absent from the index.
    # Without this, snapshot() dirs that never made it into the index grow
    # unbounded (dq05 reached 3,726 dirs / ~22G on 2026-09-30). Keep newest N.
    known = {s["id"] for s in idx["snapshots"]}
    try:
        orphans = sorted((q for q in ROOT.iterdir() if q.is_dir() and q.name not in known),
                         key=lambda q: q.name, reverse=True)
    except OSError:
        orphans = []
    swept = 0
    for q in orphans[max(0, args.keep):]:
        shutil.rmtree(q, ignore_errors=True); swept += 1
    idx["snapshots"] = keep
    _save_index(idx)
    print(f"pruned {len(drop)} snapshot(s); swept {swept} orphan(s); kept {len(keep)} "
          f"({len(protected)} good protected)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet last-known-good store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot")
    p.add_argument("--label", default="")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("mark-good")
    p.add_argument("id")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_mark_good)

    p = sub.add_parser("verify")
    p.add_argument("id")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore")
    p.add_argument("id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("maybe-good")
    p.add_argument("--clean-ticks", type=int, default=3)
    p.set_defaults(func=cmd_maybe_good)

    p = sub.add_parser("prune")
    p.add_argument("--keep", type=int, default=20)
    p.set_defaults(func=cmd_prune)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
