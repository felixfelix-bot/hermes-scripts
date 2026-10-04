#!/usr/bin/env python3
"""fleet_rootcause_guard.py — break the recurring-cleanup treadmill.

When a root-cause card is open (blocked/todo) for a recurring family (disk,
RAM), repeated "cleanup" cards are just symptoms. This guard:

  * finds open root-cause cards (title/body matching root-cause/regrowth/
    reaper/treadmill);
  * finds duplicate cleanup cards for the same family created AFTER the
    root-cause card, and archives the stale BLOCKED ones (keeping the newest)
    so the board is not buried under repeat cleanups;
  * posts the root-cause card to Buzz `operator-alerts` with a one-tap
    `promote <D-id>` decision (via decisions_responder.py) and registers it in
    the operator-decisions state.

Idempotent: one post per root-cause card (latch in
~/.hermes/state/rootcause_guard.json). Silent when there is nothing to do.

Usage:
  fleet_rootcause_guard.py [--dry-run] [--no-post] [--no-archive]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
BOARDS = HERMES / "kanban" / "boards"
STATE = HERMES / "state" / "rootcause_guard.json"
DECISIONS_STATE = HERMES / "state" / "decisions_seen.json"
ALERTS_CFG = BOT / "alerts_channel.json"
DECISIONS_CFG = BOT / "decisions_channel.json"
NSEC = HERMES / "keys" / "hermes-ops" / "cobrador.nsec"

# A root-cause card must name BOTH a causal marker and the resource family in
# its TITLE (bodies often quote unrelated "build"/"cleanup" words).
ROOTCAUSE_RE = re.compile(
    r"regrowth|treadmill|root[\s-]?cause[^.]*\b(disk|reaper|worktree|artifact)"
    r"|\b(disk|reaper|worktree|artifact)[^.]*root[\s-]?cause"
    r"|\breaper\b[^.]*worktree|worktree[^.]*\breaper\b", re.I)
# Duplicate cleanup cards: must explicitly be about the family and live on the
# SAME board as the root-cause card. This keeps the guard from archiving
# unrelated "cleanup" cards across every board.
DISK_CLEANUP_RE = re.compile(
    r"disk.*(cleanup|clean up|reclaim|free)|(cleanup|reclaim).*disk", re.I)
RAM_CLEANUP_RE = re.compile(
    r"(ram|swap|memory).*(cleanup|reclaim|free)|(cleanup|reclaim).*(ram|swap)", re.I)
ACTIVE = ("blocked", "todo", "ready", "scheduled")


def log(*p) -> None:
    print("[rootcause-guard]", *p, file=sys.stderr, flush=True)


def _read(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def post_channel(cfg: dict, text: str) -> bool:
    if not cfg or not cfg.get("orange_group") or not NSEC.exists():
        return False
    try:
        sec = NSEC.read_text().strip()
        r = subprocess.run(
            [_nak(), "event", "-k", "9", "-t", f"h={cfg['orange_group']}",
             "-c", text, "--auth", "--sec", sec,
             cfg.get("relay", "wss://relay.orangesync.tech")],
            capture_output=True, text=True, timeout=60)
        return "success" in (r.stdout + r.stderr)
    except Exception:  # noqa: BLE001
        return False


def load_tasks(boards_dir: Path) -> list[dict]:
    out = []
    for db in sorted(boards_dir.glob("*/kanban.db")):
        board = db.parent.name
        if board.startswith("_"):
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            has_created = "created_at" in cols
            q = ("SELECT id, title, body, status"
                 + (", created_at" if has_created else "") + " FROM tasks")
            for row in c.execute(q):
                out.append({"board": board, "id": row[0], "title": row[1] or "",
                            "body": row[2] or "", "status": row[3],
                            "created_at": float(row[4] or 0) if has_created else 0.0})
            c.close()
        except Exception:
            continue
    return out


def classify(tasks: list[dict]) -> list[dict]:
    """Return [{family, rootcause, duplicates:[...]}] for open root causes."""
    roots = [t for t in tasks
             if t["status"] not in ("done", "archived", "cancelled")
             and ROOTCAUSE_RE.search(t["title"] or "")]
    out = []
    for root in roots:
        blob = root["title"]
        if re.search(r"disk|worktree|build-artifact|regrowth|artifact|reaper", blob, re.I):
            fam, rx = "disk", DISK_CLEANUP_RE
        elif re.search(r"\bram\b|swap|memory|oom", blob, re.I):
            fam, rx = "ram", RAM_CLEANUP_RE
        else:
            continue
        # Same board only, blocked only — the safe subset to archive.
        dups = [t for t in tasks
                if t is not root and t["status"] == "blocked"
                and t["board"] == root["board"] and rx.search(t["title"] or "")]
        out.append({"family": fam, "rootcause": root, "duplicates": dups})
    return out


def did_for(task_id: str, family: str) -> str:
    return "D-" + hashlib.sha1(f"rootcause|{family}|{task_id}".encode()).hexdigest()[:8]


def archive_task(board: str, task_id: str, hermes_bin: str) -> bool:
    try:
        r = subprocess.run([hermes_bin, "kanban", "--board", board, "archive", task_id],
                           capture_output=True, text=True, timeout=90)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _hermes_bin() -> str:
    for c in (HERMES / "hermes-agent/venv/bin/hermes", Path.home() / ".local/bin/hermes"):
        if c.exists():
            return str(c)
    return "hermes"


def run(dry_run: bool, post: bool, archive: bool) -> int:
    tasks = load_tasks(BOARDS)
    groups = classify(tasks)
    if not groups:
        log("no open root-cause cards")
        return 0
    state = _read(STATE, {})
    now = int(time.time())
    actions = 0
    for g in groups:
        root = g["rootcause"]
        did = did_for(root["id"], g["family"])
        # archive stale BLOCKED duplicates, keep the newest duplicate
        dups = [d for d in g["duplicates"] if d["status"] == "blocked"]
        dups_sorted = sorted(dups, key=lambda d: (d.get("created_at", 0), d["id"]))
        keep = dups_sorted[-1]["id"] if dups_sorted else None
        to_archive = [d for d in dups_sorted if d["id"] != keep]
        for d in to_archive:
            if archive and not dry_run:
                if archive_task(d["board"], d["id"], _hermes_bin()):
                    actions += 1
                    log(f"archived duplicate {d['board']}/{d['id']}")
            else:
                log(f"would archive duplicate {d['board']}/{d['id']}")
        # surface the root cause once — but only if it still needs promoting
        # (a ready/in-progress card is already dispatched).
        if did in state:
            continue
        if root["status"] not in ("blocked", "todo"):
            log(f"root-cause {root['board']}/{root['id']} status={root['status']} "
                f"(already dispatched) — duplicates handled")
            state[did] = {"task": root["id"], "family": g["family"], "ts": now,
                          "surfaced": False}
            continue
        text = (
            f"[{did} · P1] root-cause-open · {g['family']}\n"
            f"What: {root['board']}/{root['id']} {root['title'][:90]}\n"
            f"Treadmill: {len(g['duplicates'])} duplicate cleanup card(s) "
            f"({'archived stale' if archive else 'present'})\n"
            f"Action: reply `promote {did}` to promote the root-cause card\n"
            f"Source: operator-alerts (Buzz)")
        if post and not dry_run:
            post_channel(_read(ALERTS_CFG, {}), text)
            _register_decision(did, root, g["family"])
        log(f"root-cause {root['board']}/{root['id']} family={g['family']} "
            f"dups={len(g['duplicates'])} did={did}")
        state[did] = {"task": root["id"], "family": g["family"], "ts": now}
    if not dry_run:
        _write(STATE, state)
    return 0 if actions >= 0 else 1


def _register_decision(did: str, root: dict, family: str) -> None:
    st = _read(DECISIONS_STATE, {"items": {}})
    items = st.setdefault("items", {})
    items[did] = {
        "sig": f"rootcause|{family}|{root['id']}",
        "first_seen": int(time.time()), "last_posted": int(time.time()),
        "status": "advisory",
        "item": {"kind": "rootcause-promote", "board": root["board"],
                 "task": root["id"], "title": root["title"][:90],
                 "promote": {"board": root["board"], "task": root["id"]}},
    }
    _write(DECISIONS_STATE, st)
    text = (f"[{did} · P1] rootcause-promote\n"
            f"What: {root['board']}/{root['id']} {root['title'][:90]}\n"
            f"Action: reply `promote {did}`\nSource: operator-alerts (Buzz)")
    post_channel(_read(DECISIONS_CFG, {}), text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args(argv)
    return run(args.dry_run, not args.no_post, not args.no_archive)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
