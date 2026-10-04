#!/usr/bin/env python3
"""cred_h5_scan.py — needle scanner behind the CRED-H5 guard (H13/H14).

Three subcommands, all reading the needle table by PATH so a literal never
appears in argv, in the process table, or in this script's output:

  home   scan $HOME (grep -F discovery + per-needle counting), classify every
         hit against ~/.git-hooks/cred-h5-policy.json, compare residue-class
         counts against the stored baseline (ceilings).
  dbs    raw-byte + logical (row level) scan of every hermes state.db.
  kdbx   KeePass magic-header scan of every git repository working tree and of
         the file names in each repository's HEAD tree.

Output is ids / counts / paths / fingerprints only. Exit 0 = clean per policy,
1 = a hard-class hit or a residue class over its ceiling, 2 = scanner unusable
(fail-closed: missing needle table, unreadable policy, no candidate discovery).

Never prints, logs or returns a literal value.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NoReturn

HOME = os.path.expanduser("~")
DEFAULT_POLICY = os.path.join(HOME, ".git-hooks", "cred-h5-policy.json")
DEFAULT_BASELINE = os.path.join(HOME, ".git-hooks", "cred-h5-baseline.json")


# --------------------------------------------------------------------- helpers
def die(msg: str, code: int = 2) -> NoReturn:
    print(f"cred_h5_scan: {msg}", file=sys.stderr)
    sys.exit(code)


def load_policy(path: str) -> dict:
    if not os.path.exists(path):
        die(f"policy not found: {path}")
    with open(path) as fh:
        pol = json.load(fh)
    if not pol.get("classes"):
        die(f"policy has no classes: {path}")
    return pol


def load_needles(pol: dict) -> list[tuple[str, bytes]]:
    """[(id, literal_bytes)] from the fail-closed needle table."""
    path = os.path.expanduser(pol["needles"])
    if not os.path.exists(path):
        die(f"needle table missing: {path}")
    out: list[tuple[str, bytes]] = []
    for line in open(path, errors="replace"):
        if line.startswith("#") or not line.strip():
            continue
        rid, _, lit = line.rstrip("\n").partition("\t")
        if rid and lit:
            out.append((rid, lit.encode()))
    if not out:
        die(f"needle table empty: {path}")
    return out


def rel_home(path: str) -> str:
    p = os.path.abspath(path)
    return p[len(HOME) + 1:] if p.startswith(HOME + os.sep) else p


def _glob_to_re(pat: str) -> re.Pattern:
    """** crosses segments, * does not, ? is one char."""
    pat = os.path.expanduser(pat)
    if pat.startswith(HOME + "/"):
        pat = pat[len(HOME) + 1:]
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def classify(pol: dict, path: str) -> dict:
    rel = rel_home(path)
    for cls in pol["classes"]:
        for g in cls["globs"]:
            if _glob_to_re(g).match(rel):
                return cls
    return {"name": "unclassified", "tier": "hard", "owner": "worker",
            "why": "default fail-closed class"}


def fp(lit: bytes) -> str:
    return hashlib.sha256(lit).hexdigest()[:12]


def count_needles(blob: bytes, needles: list[tuple[str, bytes]]) -> dict[str, int]:
    out = {}
    for rid, lit in needles:
        n = blob.count(lit)
        if n:
            out[rid] = n
    return out


# ------------------------------------------------------------------ home scan
def _scan_cmd(rg: bool, pol: dict, vals_path: str, root: str) -> list[str]:
    if rg:
        cmd = ["rg", "-l", "-F", "-f", vals_path, "--hidden", "--no-ignore"]
        for d in pol.get("prune_dirs", []):
            cmd += ["-g", f"!**/{d}/**"]
        for ig in pol.get("ignore_paths", []):
            cmd.append("-g" + "!" + os.path.basename(os.path.expanduser(ig)))
    else:
        cmd = ["grep", "-rlFI", "-f", vals_path]
        for d in pol.get("prune_dirs", []):
            cmd.append(f"--exclude-dir={d}")
        for ig in pol.get("ignore_paths", []):
            cmd.append("--exclude=" + os.path.basename(os.path.expanduser(ig)))
    cmd.append("--")
    cmd.append(root)
    return cmd


def _classify_stderr(text: str) -> tuple[int, int, list[str]]:
    """-> (transient, real, samples).

    ENOENT lines are a benign race: a file listed during the walk was deleted or
    rotated before it was read (ansible tmp dirs, SQLite -journal/-wal, cron
    output rotated by the live writer). They cannot hide a needle that is still
    on disk. Anything else (permission denied, I/O error, regex/arg errors) can.
    """
    transient = real = 0
    samples: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if not low.strip():
            continue
        if "no such file or directory" in low or "os error 2" in low:
            transient += 1
        else:
            real += 1
            if len(samples) < 5:
                samples.append(line)
    return transient, real, samples


def discover(pol: dict, root: str) -> tuple[list[str], dict]:
    """Fixed-string candidate discovery: ripgrep when available (4x faster),
    grep otherwise. Tolerates the benign ENOENT race, fails closed on any error
    that could mask a readable file."""
    needles = load_needles(pol)
    tmpd = tempfile.mkdtemp(prefix="cred-h5-")
    os.chmod(tmpd, 0o700)
    vals = os.path.join(tmpd, "needles")
    with open(vals, "wb") as fh:
        for _, lit in needles:
            fh.write(lit + b"\n")
    os.chmod(vals, 0o600)
    have_rg = shutil.which("rg") is not None
    p = None
    try:
        p = subprocess.run(_scan_cmd(have_rg, pol, vals, root),
                           capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        die("candidate discovery timed out after 1800s")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    if p is None:
        die("candidate discovery produced no result")
    if p.returncode not in (0, 1, 2):
        die(f"candidate discovery failed (scanner rc={p.returncode})")
    transient, real, samples = _classify_stderr(p.stderr.decode("utf-8", "replace"))
    meta = {"scanner": "rg" if have_rg else "grep", "rc": p.returncode,
            "transient_errors": transient, "read_errors": real, "error_samples": samples}
    if real:
        meta["incomplete"] = True
        print(f"WARNING: {real} unreadable path(s) - coverage INCOMPLETE "
              f"(cannot be reported clean)", file=sys.stderr)
        for s in samples:
            print(f"  {s}", file=sys.stderr)
    files = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l]
    ignore = {os.path.abspath(os.path.expanduser(i)) for i in pol.get("ignore_paths", [])}
    return [f for f in files if os.path.abspath(f) not in ignore], meta


def cmd_home(args) -> int:
    pol = load_policy(args.policy)
    needles = load_needles(pol)
    root = os.path.expanduser(args.root)
    t0 = time.time()
    cands, scan_meta = discover(pol, root)
    classes: dict[str, dict] = {}
    hard_rows: list[tuple[str, dict[str, int]]] = []
    total_occ = 0
    for path in cands:
        try:
            blob = open(path, "rb").read(args.max_bytes)
            if os.path.getsize(path) > args.max_bytes:
                blob = blob  # truncated read: still counts what a copy would carry
        except OSError:
            continue
        hits = count_needles(blob, needles)
        if not hits:
            continue
        cls = classify(pol, path)
        c = classes.setdefault(cls["name"], {
            "tier": cls["tier"], "owner": cls["owner"], "why": cls.get("why", ""),
            "action": cls.get("action", ""), "files": [], "occ": 0, "by_rule": {}})
        c["files"].append(rel_home(path))
        c["occ"] += sum(hits.values())
        total_occ += sum(hits.values())
        for rid, n in hits.items():
            c["by_rule"][rid] = c["by_rule"].get(rid, 0) + n
        if cls["tier"] == "hard":
            hard_rows.append((rel_home(path), hits))

    base = {}
    if os.path.exists(args.baseline):
        try:
            base = json.load(open(args.baseline)).get("classes", {})
        except Exception:  # noqa: BLE001
            base = {}

    hard_files = sum(len(c["files"]) for c in classes.values() if c["tier"] == "hard")
    hard_occ = sum(c["occ"] for c in classes.values() if c["tier"] == "hard")
    over = []
    for name, c in classes.items():
        if c["tier"] != "residue":
            continue
        ceiling = (base.get(name) or {}).get("files")
        c["ceiling"] = ceiling
        if ceiling is not None and len(c["files"]) > ceiling:
            over.append(name)

    if args.json:
        json.dump({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "root": root, "needles": len(needles), "candidates": len(cands),
            "duration_s": round(time.time() - t0, 1),
            "scan": scan_meta,
            "hard_files": hard_files, "hard_occurrences": hard_occ,
            "classes": {k: {kk: vv for kk, vv in v.items() if kk != "files"}
                        | {"files_listed": v["files"][:4000]} for k, v in classes.items()},
            "residue_over_ceiling": over,
        }, open(args.json, "w"), indent=1)

    print(f"CRED-H5 home scan   root={root}  needles={len(needles)}  "
          f"candidates={len(cands)}  {round(time.time() - t0, 1)}s")
    print(f"{'tier':<8} {'class':<24} {'files':>6} {'occ':>6} {'ceiling':>8} "
          f"{'delta':>6} owner")
    for name, c in sorted(classes.items(), key=lambda kv: (kv[1]["tier"] != "hard",
                                                           -len(kv[1]["files"]))):
        ceiling = c.get("ceiling")
        delta = "-" if ceiling is None else f"{len(c['files']) - ceiling:+d}"
        print(f"{c['tier']:<8} {name:<24} {len(c['files']):>6} {c['occ']:>6} "
              f"{'' if ceiling is None else ceiling:>8} {delta:>6} {c['owner']}")
        if c["by_rule"]:
            print(f"{'':<8}   rules: " + ", ".join(
                f"{r} x{n}" for r, n in sorted(c["by_rule"].items(), key=lambda kv: -kv[1])))
    if hard_rows:
        print(f"\nhard-class hits ({len(hard_rows)} files) — first 25:")
        for rel, hits in hard_rows[:25]:
            print(f"  {rel}   [{', '.join(f'{r} x{n}' for r, n in hits.items())}]")
    print(f"\ncoverage: scanner={scan_meta['scanner']} transient_enoent={scan_meta['transient_errors']} "
          f"read_errors={scan_meta['read_errors']}"
          + ("  -> INCOMPLETE (cannot be reported clean)" if scan_meta.get("incomplete") else "  -> complete"))
    print(f"DoD(c) verdict: hard_files={hard_files} hard_occurrences={hard_occ} "
          f"({'PASS' if hard_files == 0 else 'FAIL'})"
          + (f"  residue_over_ceiling={over}" if over else "  residue within ceilings"))

    if args.set_baseline:
        snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "note": "residue ceilings = file counts at this run; growth = finding",
                "classes": {k: {"files": len(v["files"]), "occ": v["occ"]}
                            for k, v in classes.items() if v["tier"] == "residue"}}
        json.dump(snap, open(args.baseline, "w"), indent=1)
        print(f"baseline written: {args.baseline}")

    if hard_files or over:
        return 1
    if scan_meta.get("incomplete"):
        return 3
    return 0


# ------------------------------------------------------------------- db scan
def cmd_dbs(args) -> int:
    import glob
    pol = load_policy(args.policy)
    needles = load_needles(pol)
    dbs = sorted({d for pat in pol["state_db_globs"]
                  for d in glob.glob(os.path.expanduser(pat))})
    bad = 0
    report = []
    print(f"CRED-H5 state.db scan   dbs={len(dbs)}  needles={len(needles)}")
    for db in dbs:
        raw = {}
        for rid, lit in needles:
            with open(db, "rb") as fh:
                n = 0
                while True:
                    chunk = fh.read(8 << 20)
                    if not chunk:
                        break
                    n += chunk.count(lit)
            if n:
                raw[rid] = n
        for extra in (db + "-wal", db + "-shm"):
            if os.path.exists(extra):
                for rid, lit in needles:
                    with open(extra, "rb") as fh:
                        n = fh.read().count(lit)
                    if n:
                        raw[f"{rid}(wal)"] = raw.get(f"{rid}(wal)", 0) + n
        rows = {}
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'")]
            for t in names:
                try:
                    cur = con.execute(f'SELECT * FROM "{t}"')
                    cols = [d[0] for d in cur.description]
                    for row in cur:
                        for col, val in zip(cols, row):
                            if isinstance(val, str):
                                for rid, lit in needles:
                                    if lit.decode("utf-8", "ignore") in val:
                                        rows[rid] = rows.get(rid, 0) + 1
                            elif isinstance(val, (bytes, bytearray)):
                                for rid, lit in needles:
                                    if lit in bytes(val):
                                        rows[rid] = rows.get(rid, 0) + 1
                except sqlite3.Error:
                    continue
            con.close()
        except sqlite3.Error as exc:
            print(f"  {db}: OPEN FAILED {exc}")
            bad += 1
            continue
        if raw or rows:
            bad += 1
            print(f"  {db}: RAW={raw or '{}'} LOGICAL={rows or '{}'}")
        else:
            print(f"  {db}: clean")
        report.append({"db": rel_home(db), "raw": raw, "logical": rows})
    if args.json:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "dbs": report,
                   "dirty": bad}, open(args.json, "w"), indent=1)
    print(f"state.db verdict: {'CLEAN' if bad == 0 else f'{bad} db(s) with residue'}")
    return 0 if bad == 0 else 1


# ------------------------------------------------------------------ kdbx scan
def _is_repo(d: str) -> bool:
    """True only for a WORKING repo root.

    A bare `.git` directory left behind by a failed clone/init (e.g. the stray
    /home/c03rad0r/.git) must not count: treating $HOME as a repo makes every
    file under it 'tracked-able', which both explodes the scan and turns the
    legitimate ~/secrets/*.kdbx vaults into false findings.
    """
    g = os.path.join(d, ".git")
    if os.path.isfile(g):  # worktree / submodule pointer: "gitdir: ..."
        try:
            return open(g, errors="replace").read(7) == "gitdir:"
        except OSError:
            return False
    return os.path.isfile(os.path.join(g, "HEAD")) and os.path.isdir(os.path.join(g, "objects"))


def repos(pol: dict, root: str, maxdepth: int = 5) -> list[str]:
    """Validated repository roots under root, shallowest first."""
    prune = tuple("/" + d + "/" for d in pol.get("prune_dirs", []))
    out = []
    root = root.rstrip("/")
    base_depth = root.count("/")
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if dirpath.count("/") - base_depth >= maxdepth:
            dirnames[:] = []
        if ".git" in dirnames or ".git" in filenames:
            if _is_repo(dirpath):
                out.append(dirpath)
                dirnames[:] = [d for d in dirnames if d == ".git"]
                continue
            dirnames[:] = [d for d in dirnames if d != ".git"]
        dirnames[:] = [d for d in dirnames
                       if not any(p in dirpath + "/" + d + "/" for p in prune)]
    return out


def _drop_nested(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in sorted(set(paths), key=lambda x: x.count("/")):
        if not any(p == q or p.startswith(q + "/") for q in out):
            out.append(p)
    return out


def _head_kdbx_names(pol: dict, repo: str) -> list[str]:
    try:
        p = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
                           capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return []
    if p.returncode != 0:
        return []
    return [l for l in p.stdout.decode("utf-8", "replace").splitlines()
            if l.lower().endswith((".kdbx", ".kdb"))]


def cmd_kdbx(args) -> int:
    """KeePass magic-header sweep, repo-scoped (DoD(e): 'all repos').

    A vault file is a normal, intended artifact: ~/secrets/secrets.kdbx is the
    point of the vault, not a leak. What H13(e) forbids is a vault INSIDE a
    repository - i.e. one that can be committed and pushed. So the finding set is
    'magic header reachable from a git working tree'; vaults outside any repo are
    reported as informational only.

    The magic-header test is the reliable one (a .kdbx renamed to .txt still
    matches), so every regular file in every repo is opened once and 8 bytes
    compared. Reads are threaded: pass 1 is IO-bound and the header compare is
    independent per file.
    """
    pol = load_policy(args.policy)
    magic = bytes.fromhex(pol.get("kdbx_magic", "03d9a29a67fb4bb5"))
    roots = [os.path.expanduser(r) for r in args.repos] or [HOME]
    prune = set(pol.get("prune_dirs", []))
    t0 = time.time()

    repo_roots = _drop_nested([r for root in roots for r in repos(pol, root)])
    found: list[tuple[str, str]] = []
    named: list[str] = []
    outside: list[str] = []
    seen_files = 0

    def probe(p: str) -> tuple[str, bool]:
        try:
            with open(p, "rb") as fh:
                return p, fh.read(8) == magic
        except OSError:
            return p, False

    def collect(pending: dict, cur_repo: str) -> None:
        for fut in list(pending):
            p, hit = fut.result()
            if hit:
                found.append((rel_home(p), cur_repo))
            del pending[fut]

    with ThreadPoolExecutor(max_workers=max(4, min(16, (os.cpu_count() or 4) * 2))) as ex:
        for repo in repo_roots:
            pending: dict = {}
            for dirpath, dirnames, filenames in os.walk(repo, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames if d not in prune and d != ".git"]
                for fn in filenames:
                    p = os.path.join(dirpath, fn)
                    low = fn.lower()
                    if low.endswith((".kdbx", ".kdb")):
                        named.append(rel_home(p))
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or st.st_size < 8:
                        continue
                    seen_files += 1
                    pending[ex.submit(probe, p)] = p
                    if len(pending) >= 512:
                        collect(pending, repo)
            collect(pending, repo)
            for line in _head_kdbx_names(pol, repo):
                rel = f"{rel_home(repo)}:HEAD:{line}"
                if rel not in named:
                    named.append(rel + "  (HEAD only)")

    if args.scope == "all":
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames if d not in prune and d != ".git"]
                for fn in filenames:
                    if fn.lower().endswith((".kdbx", ".kdb")):
                        outside.append(rel_home(os.path.join(dirpath, fn)))

    def tracked(repo: str, rel: str) -> bool:
        try:
            p = subprocess.run(["git", "-C", repo, "ls-files", "--error-unmatch", "--", rel],
                               capture_output=True, timeout=20)
            return p.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    print(f"CRED-H5 kdbx magic-header scan   scope={'repos' if args.scope == 'repos' else 'all'} "
          f"repos={len(repo_roots)} files_opened={seen_files} magic={magic.hex()} "
          f"{round(time.time() - t0, 1)}s")
    critical = 0
    for rel, repo in found[:40]:
        is_tracked = tracked(repo, rel)
        if is_tracked:
            critical += 1
        print(f"  {'CRITICAL (committed)' if is_tracked else 'WARN (in a repo work tree, untracked)'}"
              f"  {rel}   repo={rel_home(repo)}")
    for f in [x for x in named if "(HEAD only)" in x][:40]:
        print(f"  NAME (HEAD only) {f}")
    if outside:
        print(f"  INFO {len(outside)} vault file(s) outside any repository "
              f"(expected - not counted as findings)")
    print(f"kdbx verdict: {'CLEAN (0 vault files in repositories)' if not found else f'{len(found)} vault file(s) inside a repository ({critical} committed)'}"
          f"   name-only notes: {len(named)}")
    if args.json:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "scope": args.scope,
                   "files_opened": seen_files, "repos": len(repo_roots),
                   "magic_hits_in_repos": [{"path": r, "repo": rel_home(rp),
                                            "tracked": tracked(rp, r)} for r, rp in found],
                   "name_only": [x for x in named if "(HEAD only)" in x],
                   "outside_repos": outside,
                   "duration_s": round(time.time() - t0, 1)},
                  open(args.json, "w"), indent=1)
    return 0 if not found else 1


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["home", "dbs", "kdbx"])
    ap.add_argument("--policy", default=DEFAULT_POLICY)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--json", default=None)
    ap.add_argument("--root", default=HOME)
    ap.add_argument("--max-bytes", type=int, default=256 << 20)
    ap.add_argument("--set-baseline", action="store_true")
    ap.add_argument("--repos", nargs="*", default=[])
    ap.add_argument("--scope", choices=["repos", "all"], default="repos",
                    help="kdbx mode: 'repos' (DoD(e), findings = vaults inside a git "
                         "work tree) or 'all' (also list vaults elsewhere as INFO)")
    args = ap.parse_args()
    return {"home": cmd_home, "dbs": cmd_dbs, "kdbx": cmd_kdbx}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
