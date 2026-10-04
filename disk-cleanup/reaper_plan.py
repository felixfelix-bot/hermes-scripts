#!/usr/bin/env python3
"""reaper_plan.py — policy engine for worktree-build-artifact-reaper.sh

Reads EVERY kanban board DB, decides which per-workspace build trees
(`target/`, `node_modules/`, `.pio/`, `build/`) are safe to delete, and prints a
plan as TSV on stdout.  It NEVER deletes anything itself.

Plan line format (6 columns):
    VERDICT<TAB>size_mb<TAB>mtime<TAB>path<TAB>reason<TAB>class
VERDICT is one of DELETE / SKIP.  `--summary FILE` also writes a JSON summary.
(Column 5 stays `reason` for backwards compatibility with the wrapper, which
reads $1/$2/$4/$5; `class` was appended in 2026-09-19.)

Policy (see docs in 2026-09-13-worktree-reaper.md):
  * candidates are ONLY `<unit>/{target,node_modules,.pio,build}` — the reaper
    can never delete a unit, a repo, a worktree, a DB or any other path;
  * units come from four classes:
      terminal  — a card in a terminal status owns the path
      blocked   — cards in `blocked` (the fleet's dormancy mode); eligible only
                  with --include-blocked and untouched >= --blocked-stale-days
                  (default 30, per Felix 2026-09-13)
      orphan    — ephemeral dir under ~/worktrees, ~/.worktrees or ~/reviews
                  that no card claims
      project   — long-lived checkout in ~/repos (depth 2) or a home-root project
                  dir (depth 1); same guards, but NEVER treated as scratch
    running/ready/review/todo/unknown -> protected (--include-blocked only
    unlocks `blocked`).
  * ***CONSOLIDATION GUARD (Felix 2026-09-13, hard gate)***: before ANY build
    tree may be deleted the unit must prove its work is safe to lose:
      - `git status --porcelain` empty            (else needs-decision:dirty)
      - `git rev-list HEAD --not --remotes` empty (else needs-decision:unpushed)
      - HEAD is contained by at least one remote branch (else
        needs-decision:head-not-on-remote)
      - a non-git unit can never prove this       (needs-decision:not-a-git-repo)
    A pushed branch is also what any open/merged PR is built from, so the
    reachability test subsumes the PR check without a network call in an
    unattended nightly job.  Failing units are logged as `needs-decision` and
    surfaced, never deleted.
  * BOARD-TEXT OWNERSHIP (from t_f978b464 pass 4): a unit that no card claims via
    `workspace_path` is additionally checked against board *text* (task
    title/body/result, comments, event payloads, attachment names).  Any mention
    of the unit's basename => needs-decision:board-text-ref, never auto-delete.
    This is what keeps live review scratch (e.g. /tmp/coldrev5) safe.
  * any *active* card workspace that contains (or is contained by) a candidate
    protects it (nested-worktree case);
  * build tree must be untouched for --stale-days (hard floor 48h);
  * PROCESS guard: no live compiler/build process (cargo, rustc, tsc, go, pio,
    ninja, make, gcc, esbuild, ...) whose cwd is the unit or whose cmdline
    references it.  mtime alone is not trusted: a Rust target dir rewrites files
    continuously while building (t_8673725f measurement).
  * no process may have its cwd inside the tree, no process cmdline may
    reference it, and no open fd may exist inside it (single lsof -Fpn pass);
  * git-tracked content inside the build tree vetoes deletion;
  * symlinked build trees are never touched (they alias a real directory
    elsewhere, e.g. shared repo node_modules).

Exit codes: 0 plan produced (even if empty), 2 internal error (including "no
board DB readable at all" - a reaper that cannot see card state must never
classify units as orphans).  An individual unreadable board DB does not abort
the plan; it disables orphan eligibility instead (fail-safe).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
KANBAN = os.environ.get("REAPER_KANBAN_HOME") or os.path.join(HOME, ".hermes", "kanban")
TARGETS = ("target", "node_modules", ".pio", "build")
TERMINAL = {"done", "completed", "archived", "cancelled"}
BLOCKED = {"blocked"}
HARD_FLOOR_SECS = 48 * 3600

# home-root entries that are agent state, caches or user data — never "projects"
PROJECT_EXCLUDE = {
    ".hermes", ".cache", ".local", ".config", ".bun", ".npm", ".cargo", ".rustup",
    ".espressif", ".platformio", ".gotmp-t9fbe", ".gocache-t9fbe", "snap", ".mozilla",
    ".ssh", ".gnupg", ".worktrees", "worktrees", "reviews", "repos", "Desktop",
    "Documents", "Downloads", "Pictures", "Videos", "Music", "Public", "Templates",
    "go", "bin", ".opencode", ".ollama", ".vscode", ".cursor", ".claude", ".hermes-agent",
}

# ---- process guard -----------------------------------------------------------
# exe basename match: broad but unambiguous names
BUILD_EXE = {
    "cargo", "rustc", "rustdoc", "tsc", "go", "pio", "platformio", "ninja", "make",
    "gcc", "g++", "cc1", "cc1plus", "clang", "clang++", "ld", "as", "esbuild",
    "vite", "webpack", "next", "zig", "cmake", "ld.lld",
}
# cmdline token match: only distinctive names (short tokens like `as`/`ld`/`go`
# appear inside unrelated command lines and would veto everything)
BUILD_CMD = {"cargo", "rustc", "rustdoc", "platformio", "esbuild", "tsc", "ninja", "xtensa-esp32"}

BOARD_TEXT_QUERIES = (
    ("tasks", "SELECT id, status FROM tasks WHERE title LIKE ? OR body LIKE ? OR result LIKE ? LIMIT 5", 3),
    ("task_comments", "SELECT c.task_id, COALESCE(t.status,'?') FROM task_comments c "
                      "LEFT JOIN tasks t ON t.id = c.task_id WHERE c.body LIKE ? LIMIT 5", 1),
    ("task_events", "SELECT e.task_id, COALESCE(t.status,'?') FROM task_events e "
                    "LEFT JOIN tasks t ON t.id = e.task_id WHERE e.payload LIKE ? LIMIT 5", 1),
    ("task_attachments", "SELECT a.task_id, COALESCE(t.status,'?') FROM task_attachments a "
                         "LEFT JOIN tasks t ON t.id = a.task_id WHERE a.filename LIKE ? LIMIT 5", 1),
)


def sh(cmd, timeout=120):
    """Run a command list; return (rc, stdout, stderr). rc 124 = timeout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def board_dbs():
    out = ["%s/kanban.db" % KANBAN]
    out += sorted(glob.glob("%s/boards/*/kanban.db" % KANBAN))
    out += sorted(glob.glob("%s/boards/*/*/kanban.db" % KANBAN))
    # de-dup, keep order
    seen, uniq = set(), []
    for p in out:
        if p not in seen and os.path.exists(p):
            seen.add(p)
            uniq.append(p)
    return uniq


def load_cards():
    """Return (path -> {statuses}, path -> {kinds}, orphan_ok, unreadable)."""
    try:
        import sqlite3
    except Exception as e:  # pragma: no cover
        print("FATAL: sqlite3 unavailable: %s" % e, file=sys.stderr)
        sys.exit(2)
    cards: dict[str, set] = {}
    kinds: dict[str, set] = {}
    unreadable = []
    for db in board_dbs():
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
            rows = con.execute(
                "SELECT status, workspace_path, COALESCE(workspace_kind,'') FROM tasks "
                "WHERE workspace_path IS NOT NULL AND workspace_path <> ''").fetchall()
            con.close()
        except Exception as e:
            if "no such table: tasks" in str(e):
                continue
            unreadable.append("%s: %s" % (db, e))
            continue
        for status, path, kind in rows:
            if not path:
                continue
            p = os.path.normpath(path.rstrip("/"))
            cards.setdefault(p, set()).add((status or "?").lower())
            kinds.setdefault(p, set()).add((kind or "?").lower())
    return cards, kinds, (not unreadable), unreadable


def allowed_root(ws: str, roots) -> bool:
    ws = ws.rstrip("/")
    for r in roots:
        r = r.rstrip("/")
        if ws == r or ws.startswith(r + "/"):
            return True
    return False


def open_fd_paths():
    """One lsof pass for the whole user; return list of open path names."""
    rc, out, _ = sh(["lsof", "-w", "-u", str(os.getuid()), "-Fpn"], timeout=180)
    if rc != 0 and not out:
        return None  # unknown -> caller must skip (fail-safe)
    names = []
    for line in out.splitlines():
        if line.startswith("n") and len(line) > 1:
            names.append(line[1:])
    return names


def _proc_snapshot():
    """Yield (pid, exe_basename, cmdline, cwd) for every process of this uid."""
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = "/proc/%s" % pid
        try:
            if os.stat(base).st_uid != me:
                continue
            exe = os.path.basename(os.readlink(base + "/exe"))
        except Exception:
            continue
        try:
            cwd = os.readlink(base + "/cwd")
        except Exception:
            cwd = ""
        try:
            with open(base + "/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
        except Exception:
            cmdline = ""
        yield pid, exe, cmdline, cwd


def proc_refs(paths):
    """Return dict path -> [pid:exe] for processes whose cwd or cmdline is inside."""
    refs: dict[str, list] = {p: [] for p in paths}
    prefix = {p: p.rstrip("/") + "/" for p in paths}
    for pid, exe, cmdline, cwd in _proc_snapshot():
        for p, pre in prefix.items():
            if (cwd == p or cwd.startswith(pre)) or (p in cmdline):
                refs[p].append("%s:%s" % (pid, exe or "?"))
    return refs


def live_build_procs(units):
    """Return dict unit -> [pid:exe] when a compiler/build process is working there.

    Process-based, not mtime-based: a Rust target dir keeps rewriting files while
    `cargo` runs, so a stale-looking tree can still be mid-build.
    """
    refs: dict[str, list] = {u: [] for u in units}
    prefix = {u: u.rstrip("/") + "/" for u in units}
    for pid, exe, cmdline, cwd in _proc_snapshot():
        toks = {os.path.basename(t) for t in cmdline.split()}
        is_builder = exe in BUILD_EXE or bool(toks & BUILD_CMD)
        if not is_builder:
            continue
        for u, pre in prefix.items():
            if (cwd == u or cwd.startswith(pre)) or (u in cmdline):
                refs[u].append("%s:%s" % (pid, exe or "?"))
    return refs


def du_mb(path):
    rc, out, _ = sh(["du", "-sm", "--", path], timeout=180)
    if rc != 0:
        return None
    try:
        return int(out.split()[0])
    except Exception:
        return None


def newer_than(path, secs):
    """True if any entry inside path (or path itself) is newer than secs."""
    rc, out, _ = sh(["find", path, "-newermt", "@%d" % int(time.time() - secs),
                     "-print", "-quit"], timeout=180)
    if rc == 124:
        return None  # unknown -> fail-safe skip
    return bool(out.strip())


def git_tracked(ws, rel):
    rc, out, _ = sh(["git", "-C", ws, "ls-files", "--", rel], timeout=60)
    if rc != 0:
        return None  # not a git repo / git error -> unknown
    return bool(out.strip())


def git_workdir_clean_enough(ws):
    """True when the workspace is a git checkout (so ls-files is meaningful)."""
    rc, out, _ = sh(["git", "-C", ws, "rev-parse", "--is-inside-work-tree"], timeout=60)
    return rc == 0 and out.strip() == "true"


def _dirty_paths(unit, timeout=180):
    """Paths reported by `git status --porcelain` (None on git error)."""
    rc, out, _ = sh(["git", "-C", unit, "status", "--porcelain"], timeout=timeout)
    if rc != 0:
        return None
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if " -> " in rest:                      # rename entry
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip('"'))
    return paths


def consolidation_guard(unit, ignore_top=(), timeout=180):
    """Felix's hard gate: prove the unit's work is consolidated upstream.

    `ignore_top` is the set of build-tree directory names belonging to this unit
    (e.g. {"target","node_modules"}).  Modifications *inside* those trees are not
    "uncommitted work" — they are the regenerable artifacts this reaper exists to
    delete — and tracked content inside them is vetoed separately.  Everything
    else, including untracked files elsewhere in the checkout, counts as work.

    Returns (state, reason) with state in {"ok", "refuse", "unknown"}.
    """
    rc, out, _ = sh(["git", "-C", unit, "rev-parse", "--is-inside-work-tree"], timeout=60)
    if rc != 0 or out.strip() != "true":
        return "unknown", "not-a-git-repo"
    dirty_paths = _dirty_paths(unit, timeout=timeout)
    if dirty_paths is None:
        return "unknown", "git-status-error"
    dirty = [p for p in dirty_paths if p.split("/")[0] not in ignore_top]
    if dirty:
        return "refuse", "dirty-worktree(%d path(s))" % len(dirty)
    rc, out, _ = sh(["git", "-C", unit, "rev-list", "--count", "HEAD", "--not", "--remotes"],
                    timeout=timeout)
    if rc != 0:
        return "unknown", "git-rev-list-error"
    try:
        unpushed = int(out.strip() or 0)
    except Exception:
        return "unknown", "git-rev-list-unparsed"
    if unpushed > 0:
        return "refuse", "unpushed-commits(%d)" % unpushed
    rc, out, _ = sh(["git", "-C", unit, "branch", "-r", "--contains", "HEAD"], timeout=timeout)
    if rc != 0:
        return "unknown", "git-branch-contains-error"
    if not out.strip():
        return "refuse", "head-not-on-remote"
    return "ok", "consolidated"


def board_text_ref(basename, deadline):
    """Scan every board DB's text for `basename`.

    Returns ("clean", detail) / ("hit", "board:task") / ("unknown", why).

    Only a mention on a card that is NOT terminal protects the path: a *done*
    disk-cleanup report necessarily lists the paths it pruned, so treating every
    historical mention as a lock would make the reaper protect its own audit
    trail forever.  Live/non-terminal references (the t_f978b464 pass-4 case:
    /tmp/coldrev5 referenced only in the comments of a status=review card) do
    protect.  Historical (terminal-card) hits are reported in `detail` so they
    stay visible in the summary.

    Fail-closed: a read error or a blown time budget means unknown -> the caller
    logs needs-decision instead of deleting.
    """
    if len(basename) < 5:
        return "clean", ""            # too short to be a meaningful signal
    try:
        import sqlite3
    except Exception:
        return "unknown", "sqlite3-unavailable"
    pat = "%" + basename + "%"
    historical = []
    for db in board_dbs():
        if time.time() > deadline:
            return "unknown", "board-text-budget"
        board = os.path.basename(os.path.dirname(db)) or "root"
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
            con.execute("PRAGMA query_only=1")
            rows = []
            for _table, sql, nparams in BOARD_TEXT_QUERIES:
                try:
                    rows += con.execute(sql, (pat,) * nparams).fetchall()
                except Exception:
                    continue
            con.close()
        except Exception:
            return "unknown", "board-unreadable"
        for task_id, status in rows:
            st = (status or "?").lower()
            if st in TERMINAL:
                historical.append("%s:%s(%s)" % (board, task_id, st))
                continue
            return "hit", "%s:%s(%s)" % (board, task_id, st)
    if historical:
        return "clean", "historical:" + ",".join(historical[:3])
    return "clean", ""


def main():
    ap = argparse.ArgumentParser(description="plan a build-artifact reap")
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--blocked-stale-days", type=int, default=30,
                    help="age gate for the blocked tier (Felix 2026-09-13: 30d)")
    ap.add_argument("--include-blocked", action="store_true")
    ap.add_argument("--include-projects", dest="include_projects", action="store_true", default=True,
                    help="scan long-lived checkouts (~/repos, home-root project dirs) - default on")
    ap.add_argument("--no-projects", dest="include_projects", action="store_false")
    ap.add_argument("--allow-non-git", action="store_true",
                    help="permit units that are not git repos (default: needs-decision)")
    ap.add_argument("--min-size-mb", type=int, default=50)
    ap.add_argument("--budget-mb", type=int, default=8000)
    ap.add_argument("--board-text-budget-secs", type=int, default=180)
    ap.add_argument("--extra-root", action="append", default=[],
                    help="additional allowed workspace root (repeatable)")
    ap.add_argument("--protect-file", default=os.path.join(KANBAN, "reaper-protect.txt"))
    ap.add_argument("--summary", default=None)
    args = ap.parse_args()

    stale_days = max(2, args.stale_days)
    stale_secs = stale_days * 86400
    blocked_secs = max(2, args.blocked_stale_days) * 86400

    roots = [os.path.join(HOME, "worktrees"),
             os.path.join(KANBAN, "boards"),
             os.path.join(HOME, "reviews")]
    roots += args.extra_root
    # allow <repo>/.worktrees/<task> project worktrees via the '/.worktrees/' marker
    allow_marker = "/.worktrees/"

    protected_literals = []
    try:
        with open(os.path.expanduser(args.protect_file)) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    protected_literals.append(os.path.normpath(os.path.expanduser(line)))
    except FileNotFoundError:
        pass

    cards, kinds, orphan_ok, unreadable = load_cards()
    if not board_dbs():
        # No board state at all: every workspace would look like an "orphan"
        # and active worktrees could be reaped.  Refuse to produce a plan.
        print("FATAL: no kanban board DB under %s (wrong KANBAN root/HOME?) - "
              "refusing to plan: without card state a running workspace is "
              "indistinguishable from an orphan" % KANBAN, file=sys.stderr)
        return 2

    # ---- candidate units -----------------------------------------------------
    active_paths = set()
    blocked_paths = set()
    for ws, statuses in cards.items():
        if statuses & TERMINAL:
            continue
        if statuses & BLOCKED:
            blocked_paths.add(ws)
        else:
            active_paths.add(ws)

    def related(ws, refs):
        for a in refs:
            a = a.rstrip("/")
            if ws == a or ws.startswith(a + "/") or a.startswith(ws + "/"):
                return a
        return None

    def is_shared_dir(ws):
        return any(k and k not in ("worktree", "scratch", "") for k in kinds.get(ws, ()))

    cand_ws: list[tuple[str, str]] = []   # (unit, class)
    for ws, statuses in cards.items():
        if not allowed_root(ws, roots) and allow_marker not in (ws + "/"):
            continue
        if is_shared_dir(ws):
            cand_ws.append((ws, "skip:protected-kind(%s)" % ",".join(sorted(kinds.get(ws, ())))))
            continue
        if ws not in active_paths and ws not in blocked_paths:
            rel = related(ws, list(active_paths) + protected_literals)
            if rel:
                cand_ws.append((ws, "skip:protected(%s)" % rel))
                continue
        if ws in blocked_paths:
            cand_ws.append((ws, "blocked"))
            continue
        if ws in active_paths:
            cand_ws.append((ws, "skip:active:%s" % ",".join(sorted(statuses))))
            continue
        rel = related(ws, list(blocked_paths))
        if rel:
            cand_ws.append((ws, "skip:blocked-ws(%s)" % rel))
            continue
        if statuses & TERMINAL:
            cand_ws.append((ws, "terminal:%s" % ",".join(sorted(statuses))))
        else:
            cand_ws.append((ws, "skip:unknown-status:%s" % ",".join(sorted(statuses))))

    # orphan scan: EPHEMERAL directories under the walk roots that no card
    # references (worktree-shaped locations only - never a board workspace dir,
    # which may be a long-lived instance checkout).
    walk_roots = [os.path.join(HOME, "worktrees"),
                  os.path.join(HOME, ".worktrees"),
                  os.path.join(HOME, "reviews")]
    walked = set()
    for r in walk_roots:
        if not os.path.isdir(r):
            continue
        r = r.rstrip("/")
        for top in glob.glob("%s/*" % r) + glob.glob("%s/*/*" % r):
            base = os.path.basename(top)
            if base.startswith(".") or os.path.isfile(os.path.join(top, "kanban.db")):
                continue
            if os.path.isdir(top) and not os.path.islink(top):
                walked.add(os.path.normpath(top))
    for top in sorted(walked):
        if top in cards:
            continue
        rel = related(top, list(active_paths) + protected_literals)
        if rel:
            cand_ws.append((top, "skip:protected(%s)" % rel))
            continue
        brel = related(top, list(blocked_paths))
        if brel:
            cand_ws.append((top, "skip:blocked-ws(%s)" % brel))
            continue
        if not orphan_ok:
            cand_ws.append((top, "skip:orphan-unknown"))
            continue
        cand_ws.append((top, "orphan"))

    # project scan (2026-09-13 scope extension): long-lived checkouts that no
    # worktree pruner would ever visit.  Same guards; class != scratch.
    if args.include_projects:
        proj_scan_roots = [os.path.join(HOME, "repos")]
        for base in proj_scan_roots + [HOME]:
            if not os.path.isdir(base):
                continue
            for entry in sorted(glob.glob("%s/*" % base)):
                if os.path.islink(entry) or not os.path.isdir(entry):
                    continue
                base_name = os.path.basename(entry)
                if base_name.startswith(".") or base_name in PROJECT_EXCLUDE:
                    continue
                if os.path.normpath(entry) in cards:
                    continue
                subs = [entry] if base != HOME else [entry]
                if base == os.path.join(HOME, "repos"):
                    subs += sorted(glob.glob("%s/*" % entry))
                for unit in subs:
                    if os.path.islink(unit) or not os.path.isdir(unit):
                        continue
                    if os.path.basename(unit).startswith("."):
                        continue
                    if os.path.normpath(unit) in cards:
                        continue
                    if not any(os.path.isdir(os.path.join(unit, t)) for t in TARGETS):
                        continue
                    rel = related(unit, list(active_paths) + protected_literals)
                    if rel:
                        cand_ws.append((unit, "skip:protected(%s)" % rel))
                        continue
                    if related(unit, list(blocked_paths)):
                        cand_ws.append((unit, "skip:blocked-ws"))
                        continue
                    cand_ws.append((unit, "project"))

    # de-dup units (a unit can be reached by more than one rule); keep the most
    # permissive class actually seen, preferring eligibility classes.
    by_unit: dict[str, str] = {}
    ORDER = {"terminal": 0, "orphan": 1, "project": 2, "blocked": 3}
    for unit, why in cand_ws:
        if why.startswith("skip:"):
            by_unit.setdefault(unit, why)
            continue
        rank = ORDER.get(why.split(":")[0], 9)
        if unit in by_unit and by_unit[unit].startswith("skip:"):
            by_unit[unit] = why
            continue
        if unit not in by_unit:
            by_unit[unit] = why
        else:
            cur = by_unit[unit].split(":")[0]
            if ORDER.get(cur, 9) > rank:
                by_unit[unit] = why
    cand_ws = sorted(by_unit.items())

    # ---- per-candidate build trees ------------------------------------------
    plan = []
    for unit, why in cand_ws:
        if why.startswith("skip:"):
            # report the protected units without paying for a du on every one of
            # them (the nightly run must stay cheap); trees are listed with size 0.
            for t in TARGETS:
                p = os.path.join(unit, t)
                if os.path.isdir(p) and not os.path.islink(p):
                    plan.append(("SKIP", 0, 0, p, why, why))
            continue
        for t in TARGETS:
            p = os.path.join(unit, t)
            if not os.path.lexists(p):
                continue
            if os.path.islink(p):
                plan.append(("SKIP", 0, 0, p, "symlink", why))
                continue
            if not os.path.isdir(p):
                continue
            mt = int(os.path.getmtime(p))
            age = time.time() - mt
            if age < HARD_FLOOR_SECS:
                plan.append(("SKIP", 0, mt, p, "fresh(<48h)", why))
                continue
            if why.split(":")[0] == "blocked":
                if not args.include_blocked:
                    plan.append(("SKIP", 0, mt, p, "blocked-needs-policy", why))
                    continue
                if age < blocked_secs:
                    plan.append(("SKIP", 0, mt, p, "blocked-fresh(<%dd)" % args.blocked_stale_days, why))
                    continue
            if age < stale_secs:
                plan.append(("SKIP", 0, mt, p, "fresh(<%dd)" % stale_days, why))
                continue
            sz = du_mb(p)
            if sz is None:
                plan.append(("SKIP", 0, mt, p, "du-failed", why))
                continue
            if sz < args.min_size_mb:
                plan.append(("SKIP", sz, mt, p, "small(<%dM)" % args.min_size_mb, why))
                continue
            plan.append(("DELETE", sz, mt, p, why, why))  # provisional; guards below

    # ---- global guards -------------------------------------------------------
    ofd = open_fd_paths()
    cand_paths = [p for v, s, m, p, w, c in plan if v == "DELETE"]
    refs = proc_refs(cand_paths)

    # consolidation guard: run once per *unit* that still has a DELETE candidate
    units_with_deletes = sorted({os.path.dirname(p) for p in cand_paths})
    build_names: dict[str, set] = {}
    for p in cand_paths:
        build_names.setdefault(os.path.dirname(p), set()).add(os.path.basename(p))
    cons: dict[str, tuple] = {}
    for u in units_with_deletes:
        cons[u] = consolidation_guard(u, ignore_top=build_names.get(u, ()))

    # live build processes, keyed on the unit
    builders = live_build_procs(units_with_deletes)

    # board-text ownership for units no card claims (orphan/project)
    claimed = set(cards.keys())
    text_deadline = time.time() + max(5, args.board_text_budget_secs)
    text_cache: dict[str, tuple] = {}
    text_historical: dict[str, str] = {}

    def text_verdict(u):
        b = os.path.basename(u)
        if b not in text_cache:
            text_cache[b] = board_text_ref(b, text_deadline)
        return text_cache[b]

    final = []
    for verdict, sz, mt, p, why, cls in plan:
        if verdict != "DELETE":
            final.append((verdict, sz, mt, p, why, cls))
            continue
        unit = os.path.dirname(p)
        # 1. consolidation (Felix's hard gate) — before anything else
        state, creason = cons.get(unit, ("unknown", "unguarded"))
        if state != "ok" and not (args.allow_non_git and creason == "not-a-git-repo"):
            final.append(("SKIP", sz, mt, p, "needs-decision:%s" % creason, cls))
            continue
        # 2. ownership by board text (only for units with no card claim)
        if unit not in claimed:
            st, detail = text_verdict(unit)
            if st == "hit":
                final.append(("SKIP", sz, mt, p, "needs-decision:board-text-ref(%s)" % detail, cls))
                continue
            if st == "unknown":
                final.append(("SKIP", sz, mt, p, "needs-decision:board-text-unknown(%s)" % detail, cls))
                continue
            if detail.startswith("historical:"):
                text_historical[unit] = detail
        # 3. live builder inside the unit
        if builders.get(unit):
            final.append(("SKIP", sz, mt, p, "live-build-proc:%s" % ",".join(builders[unit][:3]), cls))
            continue
        # 4. open fds
        if ofd is None:
            final.append(("SKIP", sz, mt, p, "lsof-unavailable", cls))
            continue
        holders = [n for n in ofd if n == p or n.startswith(p.rstrip("/") + "/")]
        if holders:
            final.append(("SKIP", sz, mt, p, "open-fd", cls))
            continue
        # 5. processes with cwd/cmdline in the tree
        if refs.get(p):
            final.append(("SKIP", sz, mt, p, "live-proc:%s" % ",".join(refs[p][:3]), cls))
            continue
        # 6. content freshness
        nt = newer_than(p, stale_secs)
        if nt is None:
            final.append(("SKIP", sz, mt, p, "find-timeout", cls))
            continue
        if nt:
            final.append(("SKIP", sz, mt, p, "fresh-content(<%dd)" % stale_days, cls))
            continue
        # 7. git-tracked content inside the tree
        if git_workdir_clean_enough(unit):
            tracked = git_tracked(unit, os.path.basename(p))
            if tracked:
                final.append(("SKIP", sz, mt, p, "git-tracked-content", cls))
                continue
        final.append(("DELETE", sz, mt, p, why, cls))

    # ---- budget --------------------------------------------------------------
    budget = args.budget_mb
    spent = 0
    out = []
    for verdict, sz, mt, p, why, cls in sorted(final, key=lambda r: -r[1]):
        if verdict == "DELETE":
            if spent + sz > budget:
                out.append(("SKIP", sz, mt, p, "budget(%dM)" % budget, cls))
                continue
            spent += sz
        out.append((verdict, sz, mt, p, why, cls))

    out.sort(key=lambda r: -r[1])
    for verdict, sz, mt, p, why, cls in out:
        print("%s\t%d\t%d\t%s\t%s\t%s" % (verdict, sz, mt, p, why, cls))

    if args.summary:
        dele = [r for r in out if r[0] == "DELETE"]
        guard_counts: dict[str, int] = {}
        guard_examples: dict[str, list] = {}
        for r in out:
            if r[0] != "SKIP":
                continue
            key = r[4].split("(")[0]
            if key.startswith("needs-decision") or key.startswith("live-build-proc"):
                guard_counts[key] = guard_counts.get(key, 0) + 1
                guard_examples.setdefault(key, [])
                if len(guard_examples[key]) < 10:
                    guard_examples[key].append(r[3])
        with open(args.summary, "w") as fh:
            json.dump({
                "generated": int(time.time()),
                "stale_days": stale_days,
                "blocked_stale_days": args.blocked_stale_days,
                "include_blocked": bool(args.include_blocked),
                "include_projects": bool(args.include_projects),
                "allow_non_git": bool(args.allow_non_git),
                "min_size_mb": args.min_size_mb,
                "budget_mb": budget,
                "boards_scanned": len(board_dbs()),
                "unreadable_boards": unreadable,
                "cards_with_workspace": len(cards),
                "units_examined": len(cand_ws),
                "delete_count": len(dele),
                "delete_mb": sum(r[1] for r in dele),
                "delete_paths": [r[3] for r in dele],
                "skipped_mb": sum(r[1] for r in out if r[0] == "SKIP"),
                "guard_counts": guard_counts,
                "guard_examples": guard_examples,
                "board_text_historical_only": text_historical,
                "skip_reasons": _count([r[4].split("(")[0] for r in out if r[0] == "SKIP"]),
            }, fh, indent=2, sort_keys=True)
    return 0


def _count(items):
    c = {}
    for i in items:
        c[i] = c.get(i, 0) + 1
    return c


if __name__ == "__main__":
    sys.exit(main())
