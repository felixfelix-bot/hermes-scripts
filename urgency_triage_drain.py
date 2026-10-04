#!/usr/bin/env python3
"""urgency_triage_drain.py — classify parked-unclassified kanban tasks.

Triggered by ``urgency-triage.path`` (event: a task was parked) and
``urgency-triage.timer`` (backstop). Single-flight via flock. All unclassified
tasks found in one pass are sent to the ``urgency-triage`` profile in a single
model call (deepseek-v4-pro), which returns a strict JSON mapping; each result
is stamped (``urgency_source='classifier'``), commented, and released.

Only updates urgency — never creates tasks (no recursion).
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import sqlite3
import subprocess
import time

HOME = os.path.expanduser("~")
BOARDS = f"{HOME}/.hermes/kanban/boards"
HERMES = f"{HOME}/.hermes/hermes-agent/venv/bin/hermes"
STATE = f"{HOME}/.hermes/state"
LOCK = f"{STATE}/urgency-triage.lock"
LOG = f"{HOME}/.hermes/logs/urgency-triage.log"
LEVELS = ("now", "soon", "defer", "batch")
MAX_BATCH = 10

RUBRIC = (
    "You are the Urgency Classifier for a Hermes kanban fleet. For each task "
    "choose exactly one urgency:\n"
    "- now: blocking, production bleed, security, or an operator/reviewer "
    "explicitly-requested change; dispatch regardless of token price.\n"
    "- soon: normal work, no hard deadline; dispatch at medium-or-cheaper "
    "price. DEFAULT when unsure.\n"
    "- defer: can wait days; cheap windows only.\n"
    "- batch: lowest priority; cheapest window only.\n"
    "Be conservative: prefer soon over now unless clearly urgent; prefer soon "
    "over defer/batch unless clearly low priority. "
    'Return ONLY a JSON object with this exact shape (no prose): '
    '{"<task_id>": {"urgency": "now|soon|defer|batch", "reason": "<=15 words"}, ...}'
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%FT%TZ', time.gmtime())}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(msg, flush=True)


def collect() -> list:
    items = []
    for db in sorted(glob.glob(f"{BOARDS}/*/kanban.db")):
        board = os.path.basename(os.path.dirname(db))
        try:
            c = sqlite3.connect(db, timeout=8)
            c.execute("PRAGMA busy_timeout=6000")
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "urgency" not in cols:
                c.close()
                continue
            rows = c.execute(
                "SELECT id, title, substr(coalesce(body,''),1,600) FROM tasks "
                "WHERE status='scheduled' AND urgency IS NULL "
                "ORDER BY created_at LIMIT ?",
                (MAX_BATCH,),
            ).fetchall()
            c.close()
            for tid, title, body in rows:
                items.append({
                    "board": board, "id": tid,
                    "title": (title or "").replace("\n", " ")[:160],
                    "body": (body or "").replace("\n", " "),
                })
                if len(items) >= MAX_BATCH:
                    return items
        except Exception as e:
            log(f"collect fail-open ({board}): {e}")
    return items


def classify(items: list) -> dict:
    listing = "\n".join(
        f"{it['id']} | board={it['board']} | title={it['title']} | body={it['body']}"
        for it in items
    )
    prompt = RUBRIC + "\n\nTASKS:\n" + listing
    try:
        r = subprocess.run(
            [HERMES, "-p", "urgency-triage", "chat", "-Q", "-q", prompt],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        log("classify: model call timed out")
        return {}
    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        log(f"classify: no JSON (rc={r.returncode}) tail={out[-200:]!r}")
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        log(f"classify: bad JSON ({e}): {m.group(0)[:200]!r}")
        return {}
    return data if isinstance(data, dict) else {}


def apply_decisions(items: list, decisions: dict) -> int:
    applied = 0
    for it in items:
        d = decisions.get(it["id"])
        if isinstance(d, str):
            lvl, reason = d, ""
        elif isinstance(d, dict):
            lvl = d.get("urgency") or d.get("level")
            reason = d.get("reason", "")
        else:
            continue
        lvl = str(lvl or "").strip().lower()
        if lvl not in LEVELS:
            continue
        db = f"{BOARDS}/{it['board']}/kanban.db"
        try:
            c = sqlite3.connect(db, timeout=8)
            c.execute("PRAGMA busy_timeout=6000")
            cur = c.execute(
                "UPDATE tasks SET urgency=?, urgency_set_at=?, "
                "urgency_source='classifier' WHERE id=? AND urgency IS NULL",
                (lvl, int(time.time()), it["id"]),
            )
            c.commit()
            changed = cur.rowcount
            c.close()
            if not changed:
                continue
        except Exception as e:
            log(f"apply fail ({it['id']}): {e}")
            continue
        env = dict(os.environ, HERMES_KANBAN_BOARD=it["board"])
        rs = (str(reason) or "auto-classified")[:120]
        subprocess.run(
            [HERMES, "kanban", "--board", it["board"], "comment", it["id"],
             f"urgency-triage: {lvl} — {rs} (deepseek-v4-pro)"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        subprocess.run(
            [HERMES, "kanban", "--board", it["board"], "unblock", it["id"],
             f"classified {lvl} by urgency-triage"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        applied += 1
    return applied


def main() -> int:
    os.makedirs(STATE, exist_ok=True)
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another drain is running; exiting")
        return 0
    items = collect()
    if not items:
        return 0
    log(f"classifying {len(items)} parked task(s)")
    decisions = classify(items)
    n = apply_decisions(items, decisions)
    log(f"applied {n}/{len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
