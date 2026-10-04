#!/usr/bin/env python3
"""decisions_responder.py — act on operator commands in the D-128
`operator-decisions` Buzz channel.

Watches the channel for kind-9 messages from the operator pubkey only and
executes a small, auditable grammar against the kanban board / GitHub:

    approve <D-id>          unblock the underlying task (PR: approve-comment)
    deny    <D-id>          archive task / close PR, with a comment
    archive <D-id>          archive task (or close PR)
    merge   <D-id> confirm  merge the underlying PR (requires 'confirm')
    snooze  <D-id> <Nd>     silence escalation for N days
    promote <D-id>          promote the decision's kanban card to ready
    rebalance <D-id> <sub> [N]
                            apply a whitelisted, reversible load action on the
                            node named in the decision (sub: pause|resume|cap N|
                            drain|throttle|status)

Every command gets an audit reply in the same channel. Idempotent: deduped by
event id + persisted cursor. Replies to the operator only; never to itself.

Usage: decisions_responder.py [--once] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CHANNEL_CFG = HOME / ".hermes/bot/decisions_channel.json"
STATE = HOME / ".hermes/state/decisions_responder.json"
DECISIONS_STATE = HOME / ".hermes/state/decisions_seen.json"
DECISIONS_ANSWERED = HOME / ".hermes/state/review_decisions_answered.jsonl"
NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                        "/usr/local/bin/nak", "/usr/bin/nak"]
            if Path(c).exists()), "nak")
OPERATOR_PUB = "1a31189f46e89d327e6a4fa26376ba5fa81caaec453fab13b1c2f8245e42ba9d"
SEC_PATH = "~/.hermes/keys/hermes-ops/cobrador.nsec"
HERMES = str(HOME / ".hermes/hermes-agent/venv/bin/hermes")
REBALANCE = str(HOME / ".hermes/scripts/fleet_rebalance.py")
REBALANCE_SUBS = ("pause", "resume", "cap", "drain", "throttle", "status")


def log(*p) -> None:
    print(f"[decisions-responder] {datetime.now(timezone.utc).isoformat()}", *p,
          flush=True)


def cfg() -> dict:
    try:
        return json.loads(CHANNEL_CFG.read_text())
    except Exception:
        return {}


def key() -> str:
    return Path(os.path.expanduser(SEC_PATH)).read_text().strip()


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(p)


def run(args: list[str], timeout: int = 90) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def publish(text: str) -> None:
    c = cfg()
    if not c:
        return
    subprocess.run(
        [NAK, "event", "-k", "9", "-t", f"h={c['orange_group']}", "-c", text,
         "--auth", "--sec", key(), c["relay"]],
        capture_output=True, text=True, timeout=60)


def find_record(did: str) -> tuple[str, dict, dict] | None:
    st = load_json(DECISIONS_STATE, {})
    for kid, rec in (st.get("items") or {}).items():
        if kid == did:
            return kid, rec, st
    return None


def execute(action: str, arg: str, confirm) -> str:
    found = find_record(arg)
    if not found:
        return f"{arg}: unknown decision id"
    kid, rec, st = found
    item = rec.get("item") or {}

    if action == "snooze":
        try:
            days = float(confirm) if confirm else 1.0
        except (TypeError, ValueError):
            days = 1.0
        rec["snooze_until"] = int(time.time() + days * 86400)
        save_json(DECISIONS_STATE, st)
        return f"{kid}: snoozed {days:g}d"

    board, task = item.get("board"), item.get("task")
    repo, pr = item.get("repo"), item.get("pr")

    if action in ("archive", "deny"):
        if board and task:
            code, out = run([HERMES, "kanban", "--board", board, "archive", task])
            run([HERMES, "kanban", "--board", board, "comment", task,
                 f"operator {action} via Buzz operator-decisions ({kid})"])
            return f"{kid}: {action} task {board}/{task} -> {'ok' if code == 0 else out[-120:]}"
        if repo and pr:
            code, out = run(["gh", "pr", "close", str(pr), "-R", repo,
                             "--comment", f"Closed by operator decision {kid} via Buzz."])
            return f"{kid}: {action} PR {repo}#{pr} -> {'ok' if code == 0 else out[-120:]}"
        return f"{kid}: no actionable target"

    if action == "approve":
        if board and task:
            code, out = run([HERMES, "kanban", "--board", board, "unblock", task])
            run([HERMES, "kanban", "--board", board, "comment", task,
                 f"operator approved via Buzz operator-decisions ({kid})"])
            return f"{kid}: approve/unblock {board}/{task} -> {'ok' if code == 0 else out[-120:]}"
        if repo and pr:
            code, out = run(["gh", "pr", "comment", str(pr), "-R", repo,
                             "--body", f"Operator approved via Buzz decision {kid}."])
            return f"{kid}: approve-comment PR {repo}#{pr} -> {'ok' if code == 0 else out[-120:]}"
        return f"{kid}: no actionable target"

    if action == "promote":
        target = item.get("promote") or {}
        b = target.get("board") or item.get("board")
        t = target.get("task") or item.get("task")
        if not (b and t):
            return f"{kid}: not a promotable decision"
        code, out = run([HERMES, "kanban", "--board", b, "promote", t])
        run([HERMES, "kanban", "--board", b, "comment", t,
             f"operator promoted via Buzz ({kid})"])
        if code == 0:
            rec["status"] = "acted"
            rec["acted_at"] = int(time.time())
            save_json(DECISIONS_STATE, st)
        return f"{kid}: promote {b}/{t} -> {'ok' if code == 0 else out[-120:]}"

    if action == "merge":
        if confirm is not True:
            return f"{kid}: merge requires confirmation — reply `merge {kid} confirm`"
        if not (repo and pr):
            return f"{kid}: not a PR decision"
        code, out = run(["gh", "pr", "merge", str(pr), "-R", repo, "--squash"])
        return f"{kid}: merge {repo}#{pr} -> {'ok' if code == 0 else out[-160:]}"

    return f"{arg}: unknown action '{action}'"


def execute_rebalance(did: str, sub: str, val: str) -> str:
    """Apply a whitelisted rebalance action on the node named in the decision."""
    found = find_record(did)
    if not found:
        return f"{did}: unknown decision id"
    kid, rec, st = found
    item = rec.get("item") or {}
    node = (item.get("rebalance") or {}).get("node") or item.get("node")
    if not node:
        return f"{kid}: no rebalance target"
    if sub not in REBALANCE_SUBS:
        return (f"{kid}: bad rebalance sub {sub!r} "
                f"(allowed: {', '.join(REBALANCE_SUBS)})")
    if sub == "cap" and not val.isdigit():
        return f"{kid}: `cap` needs a non-negative integer (got {val!r})"
    cmd = [sys.executable, REBALANCE, "--node", node, sub]
    if sub == "cap":
        cmd.append(val)
    code, out = run(cmd, timeout=150)
    if code == 0:
        rec["status"] = "acted"
        rec["acted_at"] = int(time.time())
        save_json(DECISIONS_STATE, st)
    return f"{kid}: rebalance {node} {sub} -> {out[-160:]}"


def _find_by_idem(board: str, ikey: str) -> str | None:
    """Find a task id by its idempotency_key on a board (read-only)."""
    try:
        db = sqlite3.connect(
            f"file:{HOME / '.hermes/kanban/boards' / board / 'kanban.db'}?mode=ro",
            uri=True)
        cols = {c[1] for c in db.execute("PRAGMA table_info(tasks)")}
        col = "idempotency_key" if "idempotency_key" in cols else None
        if not col:
            db.close()
            return None
        row = db.execute(f"SELECT id FROM tasks WHERE {col}=?", (ikey,)).fetchone()
        db.close()
        return row[0] if row else None
    except Exception:
        return None


def execute_decide(did: str, option: str) -> str:
    """Record an operator's choice on a review-decision and promote its fix card.

    Review findings that are design/security forks are surfaced by the decisions
    digest with concrete options; this applies the choice (audit-recorded) and
    promotes the auto-emitted fix card for that track, if the emitter has run.
    """
    found = find_record(did)
    if not found:
        return f"{did}: unknown decision id"
    kid, rec, st = found
    item = rec.get("item") or {}
    kind = str(item.get("kind", ""))
    if not kind.startswith("review-decision"):
        return f"{kid}: not a review decision"
    track = kind.split("review-decision-", 1)[1]
    try:
        DECISIONS_ANSWERED.parent.mkdir(parents=True, exist_ok=True)
        with open(DECISIONS_ANSWERED, "a") as fh:
            fh.write(json.dumps({"d_id": kid, "option": option,
                                 "repo": item.get("repo"), "pr": item.get("pr"),
                                 "track": track, "ts": int(time.time())}) + "\n")
    except Exception as exc:  # noqa: BLE001
        return f"{kid}: could not record choice ({exc})"
    promo = ""
    board = item.get("board")
    ikey = f"{item.get('repo')}#{item.get('pr')}:{track}"
    if board:
        tid = _find_by_idem(board, ikey)
        if tid:
            code, out = run([HERMES, "kanban", "--board", board, "promote", tid])
            promo = f" promoted {board}/{tid} -> {'ok' if code == 0 else out[-80:]}"
        else:
            promo = " (fix card not found; emitter may not have run)"
    rec["status"] = "acted"
    rec["acted_at"] = int(time.time())
    save_json(DECISIONS_STATE, st)
    return f"{kid}: decided {option!r}{promo}"


def parse_command(text: str) -> tuple[str, str, bool] | None:
    toks = text.strip().split()
    if not toks:
        return None
    action = toks[0].lower()
    if action not in ("approve", "deny", "archive", "merge", "snooze",
                      "rebalance", "promote", "decide"):
        return None
    if len(toks) < 2:
        return None
    arg = toks[1]
    extra = toks[2:] if len(toks) > 2 else []
    confirm = any(t.lower() in ("confirm", "yes", "do-it") for t in extra)
    return action, arg, confirm


def handle(event: dict) -> None:
    text = (event.get("content") or "").strip()
    parsed = parse_command(text)
    if not parsed:
        return
    action, arg, confirm = parsed
    if action == "rebalance":
        toks = text.split()
        sub = (toks[2].lower() if len(toks) > 2 else "status")
        val = toks[3] if len(toks) > 3 else ""
        log(f"command from operator: rebalance {arg} {sub} {val}".strip())
        result = execute_rebalance(arg, sub, val)
        publish(f"[{arg}] {result}")
        log("audit posted:", result[:120])
        return
    if action == "snooze":
        toks = text.split()
        if len(toks) >= 3:
            try:
                confirm = float(toks[2].rstrip("d"))
            except ValueError:
                confirm = 1.0
        else:
            confirm = 1.0
    if action == "decide":
        option = " ".join(text.split()[2:]).strip()
        if not option:
            publish(f"[{arg}] decide needs an option, e.g. `decide {arg} reject-0`")
            return
        log(f"command from operator: decide {arg} option={option!r}")
        result = execute_decide(arg, option)
        publish(f"[{arg}] {result}")
        log("audit posted:", result[:120])
        return
    log(f"command from operator: {action} {arg} confirm={confirm}")
    result = execute(action, arg, confirm)
    publish(f"[{arg}] {result}")
    log("audit posted:", result[:120])


def fetch_new(s: dict) -> list[dict]:
    c = cfg()
    if not c:
        return []
    since = int(s.get("since", time.time() - 5))
    args = [NAK, "req", "--auth", "--sec", key(), "-k", "9",
            "-a", OPERATOR_PUB, "--since", str(since), "--limit", "100",
            c["relay"]]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log("fetch error:", exc)
        return []
    seen = set(s.get("seen", []))
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("pubkey") != OPERATOR_PUB:
            continue
        # Advance the cursor past EVERY operator event we fetched, not just the
        # ones we emit. Filtering below (group / already-seen) must not freeze
        # `since`, or a re-fetched window never moves and is re-downloaded every
        # poll forever (2026-09-30 nak CPU storm).
        try:
            _cat = int(ev.get("created_at", 0))
        except (TypeError, ValueError):
            _cat = 0
        if _cat + 1 > int(s.get("since", 0)):
            s["since"] = _cat + 1
        groups = {t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "h"}
        if c["orange_group"] not in groups:
            continue
        eid = ev.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append(ev)
    s["seen"] = list(seen)[-2000:]
    return out


def run_once(s: dict, dry_run: bool = False) -> int:
    n = 0
    for ev in fetch_new(s):
        if dry_run:
            log("would handle:", (ev.get("content") or "")[:80])
            n += 1
            continue
        handle(ev)
        n += 1
    save_json(STATE, s)
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    s = load_json(STATE, {"since": int(time.time()) - 5, "seen": []})
    if args.once:
        log(f"once: {run_once(s, args.dry_run)} command(s)")
        return 0
    c = cfg()
    log(f"watching {c.get('name','operator-decisions')} for operator commands")
    while True:
        try:
            run_once(s)
        except Exception as exc:  # noqa: BLE001
            log("loop error:", exc)
        time.sleep(20)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
