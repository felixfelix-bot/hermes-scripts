#!/usr/bin/env python3
"""triage_block_guard.py — surface `triage` cards that freeze dependent chains.

WHY: the dispatcher only runs `status='ready'`. A `todo` card is auto-promoted
to `ready` ONLY when all its parents are `done`/`archived` (kanban
`recompute_ready`, run each dispatcher tick). But `triage` is a DEAD-END:
`hermes kanban promote` rejects it ("promote only applies to 'todo' or
'blocked'"), and only an explicit `hermes kanban specify` advances it — nothing
does that automatically. So any `todo` whose ancestry reaches a `triage` card
that was never specified is deadlocked in `todo` forever: it never promotes,
never dispatches, and the review/fix it represents silently stalls (the
2026-09-16 manager-`todo` review stall: PR #1267/#1284/#1285/#1138/#1298 chains).

This guard detects `triage` cards that HAVE children (i.e. they block a chain),
older than `--min-age-h`, and ESCALATES them to the operator (revive-or-specify).
It never mutates the board — specifying runs an agent and is an operator call.

Exit 0 always (report-only). Usage:
  triage_block_guard.py [--min-age-h 6] [--json] [--dry-run] [--max-report 25]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOARDS = HERMES / "kanban" / "boards"
MIN_AGE_H = float(os.environ.get("TRIAGE_GUARD_MIN_AGE_H", "6"))
MAX_REPORT = int(os.environ.get("TRIAGE_GUARD_MAX_REPORT", "25"))


def scan(min_age_h: float, now: float | None = None) -> list[dict]:
    """triage cards with >=1 child and age >= min_age_h, oldest first."""
    now = now if now is not None else time.time()
    out: list[dict] = []
    for db in sorted(glob.glob(str(BOARDS / "*" / "kanban.db"))):
        board = os.path.basename(os.path.dirname(db))
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            c.row_factory = sqlite3.Row
            try:
                rows = c.execute(
                    "SELECT t.id, t.title, t.assignee, t.created_at, "
                    "  (SELECT COUNT(*) FROM task_links l WHERE l.parent_id=t.id) AS kids "
                    "FROM tasks t WHERE t.status='triage'").fetchall()
            finally:
                c.close()
        except sqlite3.Error:
            continue
        for r in rows:
            if int(r["kids"] or 0) < 1:
                continue
            age_h = (now - float(r["created_at"] or now)) / 3600.0
            if age_h < min_age_h:
                continue
            out.append({
                "board": board, "id": r["id"],
                "title": (r["title"] or "").strip(),
                "assignee": r["assignee"] or "",
                "kids": int(r["kids"]),
                "age_h": round(age_h, 1),
            })
    out.sort(key=lambda d: -d["age_h"])
    return out


def format_message(rows: list[dict], max_report: int = MAX_REPORT) -> str:
    shown = rows[:max_report]
    head = (f"⚠️ {len(rows)} triage card(s) block dependent chains "
            f"(unadvanced dead-ends; specify or close)")
    lines = [head]
    for r in shown:
        lines.append(f"[{r['board']}] {r['id']} · {r['age_h']:.0f}h · "
                     f"{r['kids']} child(ren) · {r['assignee'] or '-'} — {r['title'][:80]}")
    if len(rows) > len(shown):
        lines.append(f"(+{len(rows) - len(shown)} more)")
    return "\n".join(lines)


def _alert(text: str) -> str:
    try:
        sys.path.insert(0, str(HERMES / "scripts"))
        from operator_alert import post_alert  # type: ignore
        return post_alert(text, topic="triage-block-guard", cooldown_s=21600)
    except Exception:
        return "unconfigured"


# ── Bounded auto-specify (N/C2) ──────────────────────────────────────────────
# A `triage` node is a dead-end: `promote` rejects it, only `specify` advances
# it. Specify worker-assigned roots automatically (bounded), and ESCALATE the
# rest (manager/market-* owners) for a human decision.
SPECIFY_MAX = int(os.environ.get("TRIAGE_GUARD_SPECIFY_MAX", "10"))
SPECIFY_STATE = HERMES / "bot" / "triage_block_guard_state.json"


def _is_worker(assignee: str) -> bool:
    return str(assignee or "").lower().startswith("worker")


def _hermes_bin() -> str:
    p = HERMES / "hermes-agent" / "venv" / "bin" / "hermes"
    return str(p) if p.exists() else "hermes"


def _load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))
    except Exception:
        pass


def auto_specify(rows: list[dict], *, max_n: int = SPECIFY_MAX,
                 now: float | None = None, dry: bool = False) -> dict:
    """Specify worker-assigned blocking triage roots, once each, capped.

    Returns {"specified": [...], "escalate": [...], "skipped": [...]}.
    """
    now = now if now is not None else time.time()
    state = _load(SPECIFY_STATE)
    out = {"specified": [], "escalate": [], "skipped": []}
    hb = _hermes_bin()
    for r in rows:
        tid = r["id"]
        if not _is_worker(r.get("assignee")):
            out["escalate"].append({"board": r["board"], "id": tid,
                                    "assignee": r.get("assignee", ""),
                                    "title": r.get("title", "")})
            continue
        rec = state.get(tid) or {}
        if int(rec.get("attempts", 0)) >= 1:
            out["skipped"].append(tid)
            continue
        if len(out["specified"]) >= max_n:
            out["skipped"].append(tid)
            continue
        rc = 0
        if not dry:
            try:
                pr = subprocess.run(
                    [hb, "kanban", "--board", r["board"], "specify", tid],
                    capture_output=True, text=True, timeout=120,
                    stdin=subprocess.DEVNULL)
                rc = pr.returncode
            except Exception:
                rc = 1
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last"] = now
        rec["rc"] = rc
        state[tid] = rec
        out["specified"].append(tid)
    if not dry:
        _save(SPECIFY_STATE, state)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-h", type=float, default=MIN_AGE_H)
    ap.add_argument("--max-report", type=int, default=MAX_REPORT)
    ap.add_argument("--max-specify", type=int, default=SPECIFY_MAX)
    ap.add_argument("--apply", action="store_true",
                    help="actually run `kanban specify` on worker-assigned roots")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = scan(args.min_age_h)
    result = auto_specify(rows, max_n=args.max_specify, dry=args.dry_run or not args.apply)
    if args.json:
        print(json.dumps({"count": len(rows), "cards": rows[:args.max_report],
                          "specify": result}, indent=1))
    else:
        print(f"triage-block-guard: {len(rows)} blocking triage card(s) "
              f"(>= {args.min_age_h:g}h); specified={len(result['specified'])} "
              f"escalate={len(result['escalate'])}")
    if rows and not args.dry_run:
        extra = ""
        if result["escalate"]:
            extra = ("\n needs human specify/close (non-worker owner): "
                     + ", ".join(f"{e['board']}/{e['id']}" for e in result["escalate"][:10]))
        _alert(format_message(rows, args.max_report) + extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
