#!/usr/bin/env python3
"""repo_drift_check.py — live-vs-repo drift auditor (Phase L / L2).

Compares live managed files against the canonical base (`origin/master`) using
the Phase L manifest, and reports drift. authority=repo files MUST match the
base (role-deployed; drift is a clobber/revert hazard). authority=live files are
authored on the node and are never treated as failures.

Exit code: non-zero when any authority=repo file DRIFTED (unless --no-fail).

Usage:
  repo_drift_check.py [--repo DIR] [--root DIR] [--base-ref origin/master]
                      [--worktree DIR] [--manifest PATH] [--include-live]
                      [--no-fail] [--json] [--ledger PATH] [--state PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import managed_manifest as mm  # noqa: E402

IN_SYNC = "IN_SYNC"
DRIFTED_LIVE_NEWER = "DRIFTED_LIVE_NEWER"
DRIFTED_REPO_NEWER = "DRIFTED_REPO_NEWER"
MISSING_LIVE = "MISSING_LIVE"
MISSING_REPO = "MISSING_REPO"
# A live file edited after the last repo commit is an unmanaged clobber (block).
# A repo change not yet deployed is expected ("roles deploy"); warn, don't block.
FAIL_STATES = {DRIFTED_LIVE_NEWER}

# Runtime checkout states (git working trees that must be clean/on-ref).
RUNTIME_CLEAN = "RUNTIME_CLEAN"
RUNTIME_DIRTY = "RUNTIME_DIRTY"
RUNTIME_OFF_REF = "RUNTIME_OFF_REF"
RUNTIME_MISSING = "RUNTIME_MISSING"
RUNTIME_BLOCK_STATES = {RUNTIME_DIRTY, RUNTIME_OFF_REF}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True)


def repo_commit_epoch(repo: str, base_ref: str, path: str) -> int | None:
    r = _git(repo, "log", "-1", "--format=%ct", base_ref, "--", path)
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


def base_reader(repo: str, base_ref: str, worktree: str | None):
    """Return fn(repo_path)->bytes|None reading the canonical base content."""
    if worktree:
        wt = Path(worktree)
        head = _git(str(wt), "rev-parse", "HEAD")
        base = _git(repo, "rev-parse", base_ref)
        if head.returncode == 0 and base.returncode == 0 \
                and head.stdout.strip() == base.stdout.strip():
            def _read(p: str) -> bytes | None:
                f = wt / p
                return f.read_bytes() if f.is_file() else None
            return _read

    def _read_git(p: str) -> bytes | None:
        r = subprocess.run(["git", "-C", repo, "show", f"{base_ref}:{p}"],
                           capture_output=True)
        return r.stdout if r.returncode == 0 else None
    return _read_git


def audit(entries, read_base, repo: str | None = None,
          base_ref: str | None = None, include_live: bool = False) -> list[dict]:
    rows: list[dict] = []
    for mf in entries:
        if mf.authority != "repo" and not include_live:
            continue
        live = Path(os.path.expanduser(mf.live_path))
        row = {"repo_path": mf.repo_path, "live_path": str(live),
               "authority": mf.authority, "rule_id": mf.rule_id}
        if not live.is_file():
            row["state"] = MISSING_LIVE
            rows.append(row)
            continue
        base = read_base(mf.repo_path)
        if base is None:
            row["state"] = MISSING_REPO
            rows.append(row)
            continue
        live_b = live.read_bytes()
        if _sha(base) == _sha(live_b):
            row["state"] = IN_SYNC
            rows.append(row)
            continue
        row["state"] = DRIFTED_LIVE_NEWER
        if repo and base_ref:
            ct = repo_commit_epoch(repo, base_ref, mf.repo_path)
            if ct is not None and int(live.stat().st_mtime) <= ct:
                row["state"] = DRIFTED_REPO_NEWER
        row["base_sha"] = _sha(base)[:12]
        row["live_sha"] = _sha(live_b)[:12]
        row["live_bytes"] = len(live_b)
        row["base_bytes"] = len(base)
        rows.append(row)
    return rows


def summarise(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return counts


def _quarantine(checkout: dict, quarantine_dir: str) -> dict:
    """Preserve a dirty runtime checkout without mutating it.

    Writes a binary patch of tracked changes, a tar of untracked files, and a
    backup branch (created via ``git stash create`` + ``update-ref`` so the
    working tree is never touched).
    """
    path = Path(os.path.expanduser(checkout["path"]))
    cid = checkout.get("id", "checkout")
    qdir = Path(os.path.expanduser(quarantine_dir))
    qdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    art: dict = {"ts": ts}

    diff = subprocess.run(["git", "-C", str(path), "diff", "--binary", "HEAD"],
                          capture_output=True)
    if diff.returncode == 0 and diff.stdout:
        patch = qdir / f"{cid}-{ts}.patch"
        patch.write_bytes(diff.stdout)
        art["patch"] = str(patch)

    unt = subprocess.run(["git", "-C", str(path), "ls-files", "--others",
                          "--exclude-standard"], capture_output=True, text=True)
    untracked = [ln for ln in unt.stdout.splitlines() if ln.strip()]
    if untracked:
        tar = qdir / f"{cid}-{ts}-untracked.tar.gz"
        subprocess.run(["tar", "-czf", str(tar), "-C", str(path), *untracked],
                       capture_output=True)
        art["untracked_tar"] = str(tar)

    stash = subprocess.run(["git", "-C", str(path), "stash", "create"],
                           capture_output=True, text=True)
    sha = stash.stdout.strip()
    if sha:
        branch = f"backup/{socket.gethostname()}/dirty-{ts}"
        subprocess.run(["git", "-C", str(path), "update-ref",
                        f"refs/heads/{branch}", sha], capture_output=True)
        art["backup_branch"] = branch
    return art


def check_runtime_checkouts(checkouts: list[dict], quarantine: bool = False,
                            quarantine_dir: str | None = None) -> list[dict]:
    """Audit runtime git checkouts: clean tree + on the authoritative ref."""
    rows: list[dict] = []
    for rc in checkouts:
        path = Path(os.path.expanduser(rc.get("path", "")))
        row = {"id": rc.get("id"), "runtime_path": str(path),
               "blocking": bool(rc.get("blocking", True))}
        if not (path / ".git").exists():
            row["state"] = RUNTIME_MISSING
            rows.append(row)
            continue
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        row["head"] = head[:12]
        # --ignore-submodules=dirty: nested-repo (gitlink) *working-tree* dirt
        # cannot be landed in this repo — e.g. kanban task workspaces under
        # state/kanban/boards/*/t_*. Without this, a primary mirror containing
        # embedded task repos looks permanently dirty and deploy.sh refuses
        # forever. A gitlink whose recorded commit differs from the checkout is
        # still reported (that IS primary drift).
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain",
             "--ignore-submodules=dirty"],
            capture_output=True, text=True).stdout
        changed = [ln for ln in dirty.splitlines() if ln.strip()]
        ref = rc.get("authority_ref")
        off_ref = False
        if ref:
            want = subprocess.run(["git", "-C", str(path), "rev-parse", ref],
                                  capture_output=True, text=True)
            row["authority_ref"] = ref
            if want.returncode == 0 and want.stdout.strip() != head:
                off_ref = True
        if changed:
            row["state"] = RUNTIME_DIRTY
            row["dirty_count"] = len(changed)
        elif off_ref:
            row["state"] = RUNTIME_OFF_REF
        else:
            row["state"] = RUNTIME_CLEAN
        if changed and quarantine:
            row["quarantine"] = _quarantine(
                rc, quarantine_dir or str(Path.home() / ".hermes/quarantine"))
        rows.append(row)
    return rows


def _new_drift(rows: list[dict], state_path: str | None,
               extra: list[str] | None = None) -> list[str]:
    drifted = sorted(r["repo_path"] for r in rows
                     if r["state"] == DRIFTED_LIVE_NEWER)
    drifted += sorted(extra or [])
    if state_path:
        prev: list[str] = []
        try:
            prev = json.loads(Path(state_path).read_text()).get("drifted", [])
        except Exception:
            prev = []
        try:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(
                {"drifted": drifted, "checked_at": datetime.now(timezone.utc).isoformat()}))
        except OSError:
            pass
        return [p for p in drifted if p not in set(prev)]
    return drifted


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--root", default=None, help="manifest repo root (default: auto)")
    ap.add_argument("--base-ref", default="origin/master")
    ap.add_argument("--worktree", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--include-live", action="store_true")
    ap.add_argument("--no-fail", action="store_true")
    ap.add_argument("--fail-on", choices=["live-newer", "any", "none"],
                    default="live-newer",
                    help="which drift blocks (default: unmanaged live edits)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--state", default=str(Path.home() / ".hermes/bot/repo_drift_state.json"))
    ap.add_argument("--runtime", dest="runtime", action="store_true", default=True,
                    help="audit runtime_checkouts (default: on)")
    ap.add_argument("--no-runtime", dest="runtime", action="store_false")
    ap.add_argument("--runtime-gate", action="store_true",
                    help="only check runtime checkouts; exit non-zero if a blocking "
                         "one is dirty or off its authoritative ref (deploy preflight)")
    ap.add_argument("--quarantine", action="store_true",
                    help="preserve dirty runtime checkouts (patch + untracked tar + "
                         "backup branch) without mutating them")
    ap.add_argument("--quarantine-dir",
                    default=str(Path.home() / ".hermes/quarantine"))
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else mm.repo_root()
    repo = args.repo or str(root)
    if not Path(repo).exists():
        print(f"repo_drift_check: repo not found: {repo}", file=sys.stderr)
        return 2
    man = mm.load(args.manifest) if args.manifest else mm.load()

    # Runtime checkouts (gateway editable install, engine live tree) are not
    # repo->live file mappings; audit them separately.
    rt_rows: list[dict] = []
    if args.runtime or args.runtime_gate:
        rt_rows = check_runtime_checkouts(
            mm.load_runtime_checkouts(man), quarantine=args.quarantine,
            quarantine_dir=args.quarantine_dir)
    rt_block = [r for r in rt_rows
                if r.get("blocking", True) and r.get("state") in RUNTIME_BLOCK_STATES]
    # D-162: whether a dirty runtime checkout BLOCKS is the manifest's decision
    # (`blocking`), and the same decision must govern what the auditor writes to
    # the fleet-wide state file. `--runtime-gate` already honours the flag; the
    # state write did not, so a `blocking: false` checkout (hermes-bot, declared
    # report-only until its historical dirty tree is reconciled at L8) wrote
    # `runtime:hermes-bot` into repo_drift_state.json — and that file is exactly
    # what gate_engine.live_drift_paths() reads for the no_live_drift gate. One
    # report-only checkout therefore blocked the gate for EVERY code card on
    # every board (observed 2026-09-22, fleet-wide, from the moment the runtime
    # audit landed in 1349ce3e at 16:30Z), which is the opposite of what the
    # flag asks for. Non-blocking dirtiness stays fully visible: the stdout
    # report, the ledger row (`runtime: [...]` with its state) and the
    # quarantine artifacts are all unchanged. It just stops claiming the
    # authority=repo drift that the gate is actually about.
    rt_drift_ids = [f"runtime:{r['id']}" for r in rt_block]
    rt_counts = summarise([{"state": r["state"]} for r in rt_rows])

    if args.runtime_gate:
        gate_fail = bool(rt_block) and not args.no_fail
        ts = datetime.now(timezone.utc).isoformat()
        if args.json:
            print(json.dumps({"ts": ts,
                              "runtime_gate": "FAIL" if gate_fail else "PASS",
                              "counts": rt_counts, "runtime": rt_rows}, indent=2))
        else:
            for r in rt_rows:
                extra = f" ({r['dirty_count']} changed)" if r.get("dirty_count") else ""
                flag = "BLOCK" if (r.get("blocking", True)
                                   and r.get("state") in RUNTIME_BLOCK_STATES) else "    "
                print(f"  {flag} runtime {r['id']}: {r['state']}{extra}")
            if gate_fail:
                print("repo_drift_check: runtime gate FAIL — dirty/off-ref runtime "
                      "checkout. Land the change in the repo; scripts/deploy.sh "
                      "refuses until it is reconciled.", file=sys.stderr)
        return 1 if gate_fail else 0

    entries = mm.expand(man, root)
    read_base = base_reader(repo, args.base_ref, args.worktree)
    rows = audit(entries, read_base, repo=repo, base_ref=args.base_ref,
                 include_live=args.include_live)
    counts = summarise(rows)
    fail_states = {"none": set(),
                   "any": {DRIFTED_LIVE_NEWER, DRIFTED_REPO_NEWER},
                   "live-newer": {DRIFTED_LIVE_NEWER}}[args.fail_on]
    live_newer = [r for r in rows if r["state"] == DRIFTED_LIVE_NEWER
                  and r["authority"] == "repo"]
    repo_newer = [r for r in rows if r["state"] == DRIFTED_REPO_NEWER
                  and r["authority"] == "repo"]
    blocking = [r for r in rows if r["state"] in fail_states
                and r["authority"] == "repo"]
    new = _new_drift(rows, args.state, extra=rt_drift_ids)

    ts = datetime.now(timezone.utc).isoformat()
    if args.ledger:
        try:
            Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
            with open(args.ledger, "a") as fh:
                fh.write(json.dumps(
                    {"ts": ts, "base_ref": args.base_ref, "counts": counts,
                     "live_newer": [r["repo_path"] for r in live_newer],
                     "repo_newer": [r["repo_path"] for r in repo_newer],
                     "runtime": rt_rows}) + "\n")
        except OSError:
            pass

    if args.json:
        print(json.dumps({"ts": ts, "base_ref": args.base_ref, "counts": counts,
                          "fail_on": args.fail_on, "new_drift": new,
                          "runtime": rt_rows, "rows": rows}, indent=2))
    else:
        print(f"repo-drift vs {args.base_ref}: " +
              " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if new:
            print(f"NEW unmanaged live edits ({len(new)}):")
            for p in new:
                print(f"  - {p}")
        elif live_newer:
            print(f"unmanaged live edits ({len(live_newer)}):")
            for r in live_newer:
                print(f"  - {r['repo_path']}")
        if repo_newer:
            print(f"pending deploy ({len(repo_newer)}) — repo ahead, run scripts/deploy.sh:")
            for r in repo_newer:
                print(f"  - {r['repo_path']}")
        for r in rt_rows:
            if r.get("state") == RUNTIME_CLEAN:
                continue
            extra = f" ({r['dirty_count']} changed)" if r.get("dirty_count") else ""
            print(f"runtime {r['id']}: {r['state']}{extra}  {r['runtime_path']}")
            if r.get("quarantine"):
                for k, v in r["quarantine"].items():
                    if k != "ts":
                        print(f"    quarantined {k}: {v}")

    if rt_block and not args.no_fail:
        print("repo_drift_check: FAIL — dirty/off-ref blocking runtime checkout "
              f"({', '.join(r['id'] for r in rt_block)}); land the change in the "
              "repo, then fast-forward the checkout.", file=sys.stderr)
        return 1
    if blocking and not args.no_fail:
        print("repo_drift_check: FAIL — unmanaged live edits to authority=repo "
              "files; snapshot live→repo or revert live.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
