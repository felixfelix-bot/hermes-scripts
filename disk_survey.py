#!/usr/bin/env python3
"""disk_survey.py — read-only disk-usage overview + low-hanging-fruit finder.

Answers two questions for any fleet node, without changing anything:

  * "Where did the disk go?"  — filesystems, inodes, the largest directories
    under a set of roots, Docker images/volumes, journald, and deleted-but-open
    files (which `du` cannot see).
  * "What is cheap to reclaim?" — a ranked candidate list (regenerable caches,
    orphan worktrees, journal/apt/docker, unbounded telemetry DBs) with an
    estimated reclaimable size and a safety class.

Pure stdlib; never raises (each probe falls back safe); bounded by per-command
timeouts. JSON goes to ``~/.hermes/bot/disk_survey.json`` and a human summary
to stdout. Policy lives in ``state/fleet/disk_survey.json``.

Usage:
  disk_survey.py [--config PATH] [--json] [--out PATH] [--host NAME]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULTS = {
    "roots": ["~"],
    "depth": 1,
    "min_report_mb": 200,
    # Regenerable caches: safe to delete (rebuilt on demand). Globs allowed.
    "cache_dirs": [
        "~/.tmp", "~/.cache", "~/.npm", "~/.cache/pip", "~/.cache/deno",
        "~/.cache/go-build", "~/.gocache-*", "~/.gomodlocal",
        "~/.bun-cache-local", "~/.cargo/registry/cache", "~/.cache/ms-playwright",
        "~/.local/share/uv/cache", "~/.platformio/.cache", "~/.cache/uv",
    ],
    # Directories holding home/profile logs; pruned by age, not deleted whole.
    "log_dirs": ["~/.hermes/logs", "~/.hermes/profiles/*/logs",
                 "~/.hermes/profiles/*/cron/output"],
    "worktrees_dir": "~/worktrees",
    "worktree_idle_days": 7,
    "telemetry_dbs": ["~/.hermes/bot/burn_attribution.db",
                      "~/.hermes/bot/zai_usage.db"],
    "journal_keep_mb": 100,
    "top_n": 15,
    "du_timeout_s": 180,
}

SAFE = "safe-delete"          # regenerable; no data loss
QUARANTINE = "quarantine"     # move off-box (reversible)
RETAIN = "retain-prune"       # prune old rows/files inside a live store
REVIEW = "review"             # needs operator judgement


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, f"error: {e}"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _size_bytes(path: str, timeout: float) -> int:
    if not os.path.exists(path):
        return 0
    try:
        v = shutil.disk_usage(path).total  # cheap for a mountpoint
    except OSError:
        v = None
    rc, out = _run(["du", "-sb", path], timeout)
    if rc == 0 and out:
        try:
            return int(out.split()[0])
        except (ValueError, IndexError):
            return 0
    return v or 0


def _dir_size(path: str, timeout: float) -> int:
    rc, out = _run(["du", "-sb", path], timeout)
    if rc == 0 and out:
        try:
            return int(out.split()[0])
        except (ValueError, IndexError):
            pass
    return 0


def filesystems() -> list[dict]:
    out = []
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] in {"proc", "sysfs", "cgroup", "cgroup2",
                                          "devpts", "mqueue", "nsfs", "tmpfs",
                                          "overlay", "squashfs", "fusectl",
                                          "debugfs", "tracefs", "securityfs",
                                          "bpf", "configfs", "pstore",
                                          "efivarfs", "hugetlbfs"}:
            continue
        if not parts[1].startswith("/"):
            continue
        try:
            u = shutil.disk_usage(parts[1])
        except OSError:
            continue
        out.append({"mount": parts[1], "device": parts[0],
                    "size_gb": round(u.total / 1e9, 1),
                    "used_gb": round(u.used / 1e9, 1),
                    "free_gb": round(u.free / 1e9, 1),
                    "used_pct": int(round(100 * u.used / u.total)) if u.total else 0})
    return out


def top_dirs(cfg: dict) -> list[dict]:
    out = []
    depth = int(cfg.get("depth", 1))
    minb = int(cfg.get("min_report_mb", 200)) * 1_000_000
    for root in cfg.get("roots", ["~"]):
        root = _expand(root)
        if not os.path.isdir(root):
            continue
        rc, txt = _run(["du", "-x", "-d", str(depth), "-b", root],
                       float(cfg.get("du_timeout_s", 60)))
        if rc != 0:
            continue
        for ln in txt.splitlines():
            try:
                sz, p = ln.split("\t", 1)
                sz = int(sz)
            except ValueError:
                continue
            if p == root or sz < minb:
                continue
            out.append({"path": p, "mb": sz // 1_000_000})
    out.sort(key=lambda x: -x["mb"])
    return out[: int(cfg.get("top_n", 15))]


def _drop_nested(paths: list[str]) -> list[str]:
    """Keep only the outermost path when one candidate contains another."""
    keep: list[str] = []
    for p in sorted(paths):
        if any(p == q or p.startswith(q.rstrip("/") + "/") for q in keep):
            continue
        keep.append(p)
    return keep


def cache_census(cfg: dict) -> list[dict]:
    hits: dict[str, int] = {}
    for pat in cfg.get("cache_dirs", []):
        for path in glob.glob(_expand(pat)):
            if not os.path.exists(path):
                continue
            mb = _dir_size(path, float(cfg.get("du_timeout_s", 60))) // 1_000_000
            if mb >= 50:
                hits[path] = mb
    keep = set(_drop_nested(list(hits)))
    out = [{"path": p, "mb": mb} for p, mb in hits.items() if p in keep]
    out.sort(key=lambda x: -x["mb"])
    return out


def log_census(cfg: dict) -> dict:
    import time as _t
    total = 0
    stale = 0
    for pat in cfg.get("log_dirs", []):
        for d in glob.glob(_expand(pat)):
            if not os.path.isdir(d):
                continue
            for dirpath, _, files in os.walk(d):
                for f in files:
                    try:
                        p = os.path.join(dirpath, f)
                        st = os.stat(p)
                    except OSError:
                        continue
                    total += st.st_size
                    if (_t.time() - st.st_mtime) > 3 * 86400:
                        stale += st.st_size
    return {"total_mb": total // 1_000_000, "stale_gt3d_mb": stale // 1_000_000}


def worktree_census(cfg: dict) -> dict:
    root = _expand(cfg.get("worktrees_dir", "~/worktrees"))
    if not os.path.isdir(root):
        return {"total": 0, "registered": 0, "orphan": 0, "orphan_mb": 0}
    total = orphan = orphan_mb = 0
    idle_days = float(cfg.get("worktree_idle_days", 7))
    now = time.time()
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        total += 1
        registered = os.path.exists(os.path.join(d, ".git"))
        if registered:
            continue
        try:
            age = (now - os.stat(d).st_mtime) / 86400.0
        except OSError:
            age = 0
        if age >= idle_days:
            orphan += 1
            orphan_mb += _dir_size(d, float(cfg.get("du_timeout_s", 60))) // 1_000_000
    return {"total": total, "registered": total - orphan, "orphan": orphan,
            "orphan_mb": orphan_mb}


def docker_census() -> dict:
    if not shutil.which("docker"):
        return {"available": False}
    rc, out = _run(["docker", "system", "df", "--format", "{{.Type}} {{.Reclaimable}}"],
                   20)
    if rc != 0:
        return {"available": False}
    return {"available": True, "raw": out}


def journal_mb() -> int:
    if not shutil.which("journalctl"):
        return 0
    rc, out = _run(["journalctl", "--disk-usage"], 15)
    if rc != 0:
        return 0
    for tok in out.replace("take up", " ").split():
        if tok.lower().endswith(("m", "mb", "g", "gb")):
            try:
                v = float("".join(c for c in tok if c.isdigit() or c == "."))
            except ValueError:
                continue
            return int(v * 1000) if tok.lower().startswith("g") else int(v)
    return 0


def deleted_open() -> list[dict]:
    out = []
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            t = os.readlink(fd)
            if "(deleted)" not in t:
                continue
            sz = os.stat(fd).st_size
        except OSError:
            continue
        if sz > 50_000_000:
            out.append({"path": t.replace(" (deleted)", ""), "mb": sz // 1_000_000})
    out.sort(key=lambda x: -x["mb"])
    return out[:10]


def build_candidates(cfg: dict, caches: list[dict], worktrees: dict,
                     logs: dict, jmb: int) -> list[dict]:
    cands = []
    for c in caches:
        cands.append({"id": "cache", "path": c["path"], "mb": c["mb"],
                      "safety": SAFE,
                      "action": "delete (regenerable; rebuilt on demand)"})
    if worktrees.get("orphan_mb"):
        cands.append({"id": "orphan-worktrees", "path": _expand(cfg.get("worktrees_dir", "~/worktrees")),
                      "mb": worktrees["orphan_mb"], "safety": QUARANTINE,
                      "action": f"quarantine {worktrees['orphan']} orphan dirs "
                                f"(idle>={cfg.get('worktree_idle_days', 7)}d) to offload"})
    if logs.get("stale_gt3d_mb"):
        cands.append({"id": "stale-logs", "path": "profile logs/cron output",
                      "mb": logs["stale_gt3d_mb"], "safety": RETAIN,
                      "action": "delete files older than 3d"})
    if jmb > int(cfg.get("journal_keep_mb", 100)):
        cands.append({"id": "journald", "path": "/var/log/journal",
                      "mb": jmb - int(cfg.get("journal_keep_mb", 100)),
                      "safety": SAFE,
                      "action": f"journalctl --vacuum-size={cfg.get('journal_keep_mb', 100)}M"})
    for db in cfg.get("telemetry_dbs", []):
        db = _expand(db)
        if os.path.exists(db):
            mb = os.path.getsize(db) // 1_000_000
            if mb >= 200:
                cands.append({"id": "telemetry-db", "path": db, "mb": mb,
                              "safety": RETAIN,
                              "action": "telemetry_retention.py --apply (bound + VACUUM)"})
    cands.sort(key=lambda x: -x["mb"])
    return cands


def survey(cfg: dict, host: str) -> dict:
    caches = cache_census(cfg)
    worktrees = worktree_census(cfg)
    logs = log_census(cfg)
    jmb = journal_mb()
    report = {
        "ts": int(time.time()),
        "host": host,
        "filesystems": filesystems(),
        "top_dirs": top_dirs(cfg),
        "caches": caches,
        "logs": logs,
        "worktrees": worktrees,
        "docker": docker_census(),
        "journal_mb": jmb,
        "deleted_open": deleted_open(),
        "candidates": build_candidates(cfg, caches, worktrees, logs, jmb),
    }
    report["reclaimable_mb"] = sum(c["mb"] for c in report["candidates"])
    return report


def render(rep: dict) -> str:
    L = [f"disk survey {rep['host']} @ {time.strftime('%Y-%m-%d %H:%M', time.localtime(rep['ts']))}"]
    for fs in rep["filesystems"]:
        L.append(f"  {fs['mount']:<16} {fs['used_gb']:>6}G / {fs['size_gb']}G "
                 f"({fs['used_pct']}%, {fs['free_gb']}G free)")
    for d in rep["top_dirs"][:8]:
        L.append(f"  top: {d['mb']:>6}M  {d['path']}")
    wt = rep["worktrees"]
    L.append(f"  worktrees: {wt['total']} dirs, {wt['orphan']} orphan "
             f"(~{wt['orphan_mb']}M reclaimable)")
    L.append(f"  journal: {rep['journal_mb']}M; "
             f"reclaimable now ~{rep['reclaimable_mb']}M")
    for c in rep["candidates"][:10]:
        L.append(f"  [{c['safety']:<13}] {c['mb']:>6}M  {c['path']}  -> {c['action']}")
    if rep["deleted_open"]:
        L.append(f"  deleted-but-open: {len(rep['deleted_open'])} file(s) "
                 f"~{sum(x['mb'] for x in rep['deleted_open'])}M (held by processes)")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--out", default="")
    ap.add_argument("--host", default=os.uname().nodename)
    args = ap.parse_args(argv)

    cfg = dict(DEFAULTS)
    for p in [args.config, _expand("~/.hermes/bot/disk_survey.json")]:
        if p and os.path.exists(p):
            try:
                cfg.update(json.loads(Path(p).read_text()))
                break
            except (OSError, ValueError):
                pass

    rep = survey(cfg, args.host)
    out = Path(_expand(args.out)) if args.out else \
        Path(_expand("~/.hermes/bot/disk_survey.json"))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1) + "\n")
    except OSError:
        pass
    print(json.dumps(rep, indent=1) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
