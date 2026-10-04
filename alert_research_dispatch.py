#!/usr/bin/env python3
"""alert_research_dispatch.py — never hit the same problem twice (D-128 §18).

Whenever a fleet alert is raised, schedule a research task that works with the
consultants to produce a DURABLE improvement recommendation (root cause + the
fix that stops recurrence), not just another cleanup.

Sources: `unified-system-alert` state categories, the `anomaly_events` table,
and `fleet_interventions.jsonl` actions.

Guardrails: one open research task per family; 24h snooze after a no-action
verdict; max ~2 concurrent; suppressed when a root-cause card for the family is
already open. Dry-run by default; `--apply` dispatches.

Usage: alert_research_dispatch.py [--dry-run] [--since T] [--json]
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
STATE = HERMES / "state" / "alert_research.json"
ALERTS_STATE = Path(os.environ.get("USA_STATE", "/tmp/unified-system-alert-state.json"))
USAGE_DB = BOT / "zai_usage.db"
LEDGER = BOT / "fleet_interventions.jsonl"
NSEC = HERMES / "keys" / "hermes-ops" / "cobrador.nsec"
ALERTS_CFG = BOT / "alerts_channel.json"
DECISIONS_CFG = BOT / "decisions_channel.json"
DECISIONS_STATE = HERMES / "state" / "decisions_seen.json"

MAX_CONCURRENT = 2
SNOOZE_S = 86400
CONSULTANT_MODEL = "deepseek-v4-pro"
CONSULTANT_BOARD = "hermes-orchestration"
RESEARCH_SIZE = 128000  # model context registry floor (informational)


def log(*p) -> None:
    print("[alert-rca]", *p, file=sys.stderr, flush=True)


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


# ── collection ────────────────────────────────────────────────────────────────

def collect(since: float) -> list[dict]:
    """New alerts since `since`, normalized to {family, severity, evidence}."""
    out: list[dict] = []
    # 1) unified-system-alert categories
    st = _read(ALERTS_STATE, {})
    for fam, info in (st.get("alerts") or {}).items():
        sev = (info or {}).get("severity") if isinstance(info, dict) else None
        if sev:
            out.append({"family": fam.upper(), "severity": sev,
                        "evidence": f"unified-system-alert {fam}={sev}"})
    # 2) anomaly_events
    if USAGE_DB.exists():
        try:
            c = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=5)
            for ts, sev, cat, title in c.execute(
                    "SELECT ts, severity, category, title FROM anomaly_events "
                    "WHERE ts >= ? ORDER BY ts DESC LIMIT 200", (since,)):
                out.append({"family": (cat or "anomaly").upper(),
                            "severity": (sev or "WARN").lower(),
                            "evidence": f"{cat}: {title}"})
            c.close()
        except sqlite3.Error:
            pass
    # 3) fleet_interventions (arbiter actions)
    try:
        for line in LEDGER.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if float(e.get("ts", 0)) >= since and e.get("action"):
                out.append({"family": f"ARBITER-{e['action']}".upper(),
                            "severity": "high",
                            "evidence": f"arbiter {e['action']} on {e.get('target','?')}"})
    except OSError:
        pass
    # dedupe by family (keep first/most severe)
    by_fam: dict[str, dict] = {}
    for a in out:
        by_fam.setdefault(a["family"], a)
    return list(by_fam.values())


# ── gating ────────────────────────────────────────────────────────────────────

def due_families(alerts: list[dict], state: dict, now: float,
                 max_concurrent: int = MAX_CONCURRENT,
                 snooze_s: int = SNOOZE_S) -> list[dict]:
    """Pure: families that should get a research task now."""
    fams = state.setdefault("families", {})
    open_count = sum(1 for f in fams.values() if f.get("status") == "open")
    due = []
    for a in alerts:
        fam = a["family"]
        rec = fams.get(fam) or {}
        if rec.get("status") == "open":
            continue
        snooze_until = float(rec.get("snooze_until", 0) or 0)
        if snooze_until > now:
            continue
        if open_count >= max_concurrent:
            break
        by_fam = a
        due.append(by_fam)
        open_count += 1
    return due


def _open_rootcause(family: str) -> bool:
    """True when an open root-cause/improvement card exists FOR THIS family."""
    token = family.split("-")[0].lower()
    for db in BOARDS.glob("*/kanban.db"):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            row = c.execute(
                "SELECT 1 FROM tasks WHERE status NOT IN ('done','archived','cancelled') "
                "AND title LIKE ? "
                "AND (title LIKE '%root-cause%' OR title LIKE '%root cause%' "
                "     OR title LIKE '%regrowth%' OR title LIKE '%treadmill%') "
                "LIMIT 1", (f"%{token}%",)).fetchone()
            c.close()
            if row:
                return True
        except sqlite3.Error:
            continue
    return False


# ── dispatch + surface ────────────────────────────────────────────────────────

def _consultant_prompt(alert: dict, report: str) -> str:
    return (
        "You are a fleet reliability consultant. An alert family is recurring: "
        f"{alert['family']} (severity {alert['severity']}). Evidence: "
        f"{alert['evidence']}. The goal is to STOP it recurring, not to clean up. "
        "(1) Verdict: real defect or false positive? with evidence. "
        "(2) Root cause. (3) ONE durable improvement recommendation (the change "
        "that makes this class of alert stop) — or a reasoned `no-action`. "
        "(4) If actionable, create ONE scoped fix card (TDD, cross-family review) "
        f"and reply with its id. Write findings to {report}.")


def _hermes() -> str:
    for c in (HERMES / "hermes-agent/venv/bin/hermes", Path.home() / ".local/bin/hermes"):
        if c.exists():
            return str(c)
    return "hermes"


def dispatch(alert: dict, now: float) -> str | None:
    import sqlite3 as _sq
    ts = time.strftime("%Y%m%d-%H%M", time.gmtime(now))
    report = str(Path.home() / "reports" / f"alert-rca-{alert['family'].lower()}-{ts}.md")
    Path(report).parent.mkdir(parents=True, exist_ok=True)
    title = f"[ALERT-RCA] {alert['family']} — durable improvement consultant"
    body = _consultant_prompt(alert, report)
    try:
        r = subprocess.run([_hermes(), "kanban", "--board", CONSULTANT_BOARD,
                            "create", "--body", body, "--urgency", "soon",
                            "--json", title],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"\bt_[a-f0-9]{8}\b", r.stdout)
        tid = m.group(0) if m else None
        if not tid:
            if r.returncode != 0:
                log("kanban create failed:", (r.stderr or r.stdout).strip()[:160])
            return None
        db = BOARDS / CONSULTANT_BOARD / "kanban.db"
        if db.exists():
            c = _sq.connect(str(db), timeout=5)
            c.execute("update tasks set model_override=? where id=?", (CONSULTANT_MODEL, tid))
            c.commit(); c.close()
        return tid
    except Exception as exc:  # noqa: BLE001
        log("dispatch error:", exc)
        return None


# ── surfacing + close-the-loop ───────────────────────────────────────────────

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


def register_decision(family: str, task: str, now: float) -> str:
    did = "D-" + hashlib.sha1(f"alertrca|{family}|{task}".encode()).hexdigest()[:8]
    st = _read(DECISIONS_STATE, {"items": {}})
    items = st.setdefault("items", {})
    items[did] = {"sig": f"alertrca|{family}", "first_seen": int(now),
                  "last_posted": int(now), "status": "advisory",
                  "item": {"kind": "alert-rca", "board": CONSULTANT_BOARD,
                           "task": task, "title": f"RCA {family}",
                           "promote": {"board": CONSULTANT_BOARD, "task": task}}}
    _write(DECISIONS_STATE, st)
    post_channel(_read(DECISIONS_CFG, {}),
                 f"[{did} · P1] alert-rca · {family}\n"
                 f"Card: {task}\nAction: reply `promote {did}`\n"
                 f"Source: operator-alerts (Buzz)")
    return did


def _task_status(board: str, tid: str) -> str:
    db = BOARDS / board / "kanban.db"
    if not db.exists():
        return ""
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        c.close()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def resolve_closed(state: dict) -> int:
    """Clear a family latch once its research/fix card is done or archived."""
    n = 0
    for fam, rec in (state.get("families") or {}).items():
        if rec.get("status") != "open":
            continue
        st = _task_status(CONSULTANT_BOARD, rec.get("task", ""))
        if st in ("done", "archived"):
            rec["status"] = "resolved"
            rec["resolved_at"] = int(time.time())
            n += 1
    return n


def run(apply: bool) -> int:
    now = time.time()
    state = _read(STATE, {})
    since = float(state.get("last_check", now - 3600) or (now - 3600))
    alerts = collect(since)
    due = due_families(alerts, state, now)
    fams = state.setdefault("families", {})
    if apply:
        resolve_closed(state)
    actions = 0
    for a in due:
        if _open_rootcause(a["family"]):
            log(f"skip {a['family']}: open root-cause card")
            continue
        if not apply:
            log(f"would dispatch RCA for {a['family']} ({a['severity']})")
            continue
        tid = dispatch(a, now)
        if tid:
            did = register_decision(a["family"], tid, now)
            post_channel(_read(ALERTS_CFG, {}),
                         f"🔎 ALERT-RCA dispatched for {a['family']}: {tid} — "
                         f"durable-improvement consultant")
            fams[a["family"]] = {"status": "open", "task": tid, "ts": now,
                                 "decision": did}
            actions += 1
            log(f"dispatched RCA {tid} for {a['family']} ({did})")
    state["last_check"] = now
    if apply:
        _write(STATE, state)
    return actions


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    n = run(apply=args.apply and not args.dry_run)
    if args.json:
        print(json.dumps({"dispatched": n}))
    else:
        print(f"[alert-rca] dispatched {n} research task(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
