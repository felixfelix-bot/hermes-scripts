#!/usr/bin/env python3
"""guarded_reaper.py — reclaim build artifacts from abandoned worktrees.

Approved policy (2026-09-13): delete ``target`` / ``node_modules`` / ``.pio``
at the ROOT of a worktree whose kanban card is ``blocked`` and untouched for
>= ``idle_days``. This is the root-cause fix for the disk treadmill: the
regrowth writer is ``worktrees/*/target`` (~4 G and +2%/day), not the sweep.

Worktrees in this Hermes version are NOT linked to cards via
``tasks.workspace_path`` (that column is empty). They are named either
``t_<task_id>`` (card-linkable) or by branch label (``mf-consolidate-amp``).
Card-linked worktrees are reaped under the approved policy; orphaned
(non-``t_``) worktrees are only touched when ``orphan_tier_enabled`` is set
(default OFF — deliberately outside the approved scope).

Hard safety rails:
  * only the exact artifact dir names in the policy, only at the worktree root;
  * never follows a symlink;
  * worktree must live under the policy ``worktrees_dir``;
  * card-linked: skipped if the card is not in ``statuses``, or looks live
    (claim_lock / live worker_pid / recent heartbeat);
  * orphan tier (opt-in): requires filesystem-idle >= ``orphan_idle_days``;
  * the whole run ABORTS when the node is busy: load1 > load_ceiling OR
    MemAvailable < mem_floor_mb (never competes with real work);
  * --dry-run writes a manifest and deletes nothing.

Modes:
  guarded_reaper.py --dry-run     # manifest only (default)
  guarded_reaper.py --apply       # delete + manifest + intervention ledger

Policy: ~/.hermes/bot/reaper_policy.json (seeded, operator-editable).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
POLICY = BOT / "reaper_policy.json"
BOARDS = HERMES / "kanban" / "boards"
REPORTS = Path.home() / "reports"
LEDGER = BOT / "fleet_interventions.jsonl"

DEFAULT_POLICY = {
    "blocked_tier_enabled": True,
    "idle_days": 14,
    "min_size_mb": 50,
    "artifact_dirs": ["target", "node_modules", ".pio"],
    "statuses": ["blocked", "archived", "cancelled", "done"],
    "worktrees_dir": "~/worktrees",
    "orphan_tier_enabled": False,
    "orphan_idle_days": 30,
    "load_ceiling": 3.0,
    "mem_floor_mb": 1500,
    # Disk-pressure override (2026-09-28): a full disk is itself a cause of the
    # load the gate waits for, so the reaper must not deadlock behind it. At or
    # above this used-%, the load gate is bypassed (the memory floor is kept
    # unless `disk_emergency_bypass_mem`).
    "disk_emergency_pct": 88.0,
    "disk_emergency_bypass_mem": False,
}


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def load_policy() -> dict:
    pol = dict(DEFAULT_POLICY)
    pol.update(_read(POLICY, {}) or {})
    return pol


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 10 ** 9


def _load1() -> float:
    try:
        return float(os.getloadavg()[0])
    except OSError:
        return 0.0


def _disk_used_pct(pol: dict) -> float:
    """Used-% of the filesystem holding the worktrees (or /). 0.0 on error."""
    try:
        base = os.path.expanduser(pol.get("worktrees_dir") or "~")
        path = base if os.path.exists(base) else "/"
        st = os.statvfs(path)
        if not st.f_blocks:
            return 0.0
        return (1.0 - (st.f_bavail / st.f_blocks)) * 100.0
    except Exception:
        return 0.0


def _disk_emergency(pol: dict) -> str | None:
    """Non-None reason string when the disk is at/above the emergency band."""
    thr = float(pol.get("disk_emergency_pct", 0) or 0)
    if thr <= 0:
        return None
    pct = _disk_used_pct(pol)
    return f"{pct:.0f}% >= {thr:.0f}%" if pct >= thr else None


def resource_ok(pol: dict) -> tuple[bool, str]:
    """Whether the reaper may run now.

    Disk-pressure override: when the disk is critically full, the load gate is
    BYPASSED — the disk pressure is what keeps the load high, so waiting on
    load here is a deadlock (the 2026-09-28 self-lock). The memory floor is
    still honoured (deleting needs RAM) unless `disk_emergency_bypass_mem`.
    """
    mem = _mem_available_mb()
    emergency = _disk_emergency(pol)
    if emergency:
        if mem < int(pol["mem_floor_mb"]) and not pol.get("disk_emergency_bypass_mem", False):
            return False, f"disk emergency ({emergency}) but mem {mem}MB < {pol['mem_floor_mb']}MB"
        return True, f"disk emergency ({emergency}) — load gate bypassed"
    load = _load1()
    if load > float(pol["load_ceiling"]):
        return False, f"load {load:.2f} > {pol['load_ceiling']}"
    if mem < int(pol["mem_floor_mb"]):
        return False, f"mem {mem}MB < {pol['mem_floor_mb']}MB"
    return True, "ok"


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False
    except PermissionError:
        return True


def eligible(task: dict, pol: dict, now: float) -> tuple[bool, str]:
    """Pure: is this CARD-LINKED worktree reclaimable under the policy?"""
    if not pol.get("blocked_tier_enabled", True):
        return False, "policy-disabled"
    status = task.get("status")
    if status not in set(pol.get("statuses", [])):
        return False, f"status={status}"
    if task.get("claim_lock"):
        return False, "claimed"
    if _pid_alive(task.get("worker_pid")):
        return False, "live-worker-pid"
    last = max(float(task.get(k) or 0) for k in
               ("created_at", "started_at", "completed_at", "last_heartbeat_at"))
    idle_s = now - last
    if idle_s < int(pol["idle_days"]) * 86400:
        return False, f"idle={idle_s / 86400:.1f}d<{pol['idle_days']}d"
    return True, "eligible"


def fs_idle_days(path: Path) -> float:
    """Days since the worktree root was last touched, ignoring artifact dirs."""
    arts = set(DEFAULT_POLICY["artifact_dirs"])
    newest = path.stat().st_mtime if path.exists() else 0.0
    try:
        for child in path.iterdir():
            if child.name in arts:
                continue
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return (time.time() - newest) / 86400.0 if newest else 0.0


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        if Path(dirpath).is_symlink():
            dirnames[:] = []
            continue
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                continue
    return total


def collect_artifacts(ws: Path, pol: dict, min_mb: int) -> list[dict]:
    out = []
    for name in pol.get("artifact_dirs", []):
        p = ws / name
        if not p.is_dir() or p.is_symlink():
            continue
        mb = _dir_size(p) >> 20
        if mb >= min_mb:
            out.append({"path": str(p), "mb": mb})
    return out


def load_tasks(boards_dir: Path) -> dict[str, dict]:
    """Map task id -> task dict across all boards (for t_<id> worktree linking)."""
    tasks: dict[str, dict] = {}
    for db in sorted(glob.glob(str(boards_dir / "*" / "kanban.db"))):
        if Path(db).parent.name.startswith("_"):
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            sel = ["id", "status"]
            for extra in ("created_at", "started_at", "completed_at",
                          "last_heartbeat_at", "worker_pid", "claim_lock"):
                sel.append(extra if extra in cols else f"NULL AS {extra}")
            for row in c.execute(f"SELECT {', '.join(sel)} FROM tasks"):
                t = dict(zip(sel, row))
                tasks[t["id"]] = t
            c.close()
        except Exception:
            continue
    return tasks


def scan(worktrees: Path, tasks: dict, pol: dict, now: float) -> list[dict]:
    """Return reclaimable items: card-linked t_<id> + optional orphan tier."""
    items: list[dict] = []
    min_mb = int(pol["min_size_mb"])
    if not worktrees.is_dir():
        return items
    for child in sorted(worktrees.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        arts = collect_artifacts(child, pol, min_mb)
        if not arts:
            continue
        name = child.name
        if name.startswith("t_"):
            task = tasks.get(name)
            if not task:
                continue
            ok, why = eligible(task, pol, now)
            if not ok:
                continue
            items.append({"tier": "blocked-card", "board": task.get("board"),
                          "task": name, "workspace": str(child),
                          "artifacts": arts, "mb": sum(a["mb"] for a in arts),
                          "why": why})
        elif pol.get("orphan_tier_enabled"):
            idle = fs_idle_days(child)
            if idle < float(pol.get("orphan_idle_days", 30)):
                continue
            items.append({"tier": "orphan", "board": None, "task": name,
                          "workspace": str(child), "artifacts": arts,
                          "mb": sum(a["mb"] for a in arts),
                          "why": f"orphan fs-idle={idle:.1f}d"})
    items.sort(key=lambda x: -x["mb"])
    return items


def write_manifest(items: list[dict], now: float, applied: bool) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    path = REPORTS / f"reaper-blocked-tier-{ts}.json"
    path.write_text(json.dumps({
        "ts": int(now), "applied": applied,
        "reclaimed_mb": sum(i["mb"] for i in items), "items": items,
    }, indent=1) + "\n")
    return path


def _ledger(entry: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="ignore the resource gate (operator override)")
    ap.add_argument("--worktrees", default="")
    ap.add_argument("--boards", default=str(BOARDS))
    args = ap.parse_args(argv)

    now = time.time()
    pol = load_policy()
    worktrees = Path(args.worktrees).expanduser() if args.worktrees \
        else Path(str(pol["worktrees_dir"])).expanduser()

    ok, why = resource_ok(pol)
    if not ok and not args.force:
        print(f"[reaper] resource-gated: {why} — no action")
        return 0

    items = scan(worktrees, load_tasks(Path(args.boards)), pol, now)
    if not items:
        print("[reaper] nothing eligible")
        return 0

    applied = bool(args.apply) and not args.dry_run
    if applied:
        for it in items:
            for a in it["artifacts"]:
                try:
                    shutil.rmtree(a["path"])
                except OSError as exc:
                    a["error"] = str(exc)
            _ledger({"ts": now, "actor": "guarded-reaper", "task": it["task"],
                     "workspace": it["workspace"], "tier": it["tier"],
                     "reclaimed_mb": it["mb"]})
    manifest = write_manifest(items, now, applied)
    total = sum(i["mb"] for i in items)
    verb = "reclaimed" if applied else "would reclaim"
    print(f"[reaper] {verb} {total} MB across {len(items)} worktree(s); "
          f"manifest={manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
