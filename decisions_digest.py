#!/usr/bin/env python3
"""decisions_digest.py — single aggregator for operator decisions (D-128).

Sweeps every "needs Felix" source into one deduped queue and posts each item
ONCE to the OrangeSync `operator-decisions` channel (kind 9). Re-posts only when
an item's state changes; escalates aged items instead of repeating the list.

Sources:
  * operator-action blocks across all kanban boards (blocked + triage)
  * superseded cards still parked (block reason starts SUPERSEDED)
  * open felixfelix-bot PRs needing a decision (approved+mergeable / changes-requested)
  * non-done cards on the `inbound` board

State: ~/.hermes/state/decisions_seen.json
Health: ~/.hermes/state/decisions_post_health.json
Channel: ~/.hermes/bot/decisions_channel.json

FAILURE SEMANTICS (2026-09-23, task t_a328b9a0): delivery state is advanced
ONLY for items that actually posted. A failed post leaves the item eligible so
the next tick retries it — before this, state was written *before* posting, so a
failed post was recorded as delivered and the decision was lost for
`--escalate-hours` (or forever). Every failure is also written to the health file
and printed with an ALERT/POST-FAIL marker so the anomaly channel (anomaly-dm-
digest.sh, block for this job) surfaces it to the operator. Exit is ALWAYS 0:
this is a `no_agent` cron and a non-zero exit is itself delivered as a failure.

WRITER vs OBSERVER (2026-09-27, task t_a328b9a0): the bridge identity used to be
hardcoded to *another node's* key path (`~/.hermes/keys/hermes-ops/cobrador.nsec`).
On every other fleet node — which legitimately does not hold that key — the job
therefore reported `posted 0/N` plus `ALERT decision-channel-degraded` on every
single tick: 110 consecutive failures on dq05 that described a *healthy*
channel and got filed as a relay outage. The identity is now resolved from this
node's own ops config (`hermes_ops.json` → `node_nsec`), and the node's role is
derived from the channel's member list:

  writer   this node's pubkey is a channel member → posts, writes state.
  observer this node's pubkey is NOT a member → never posts, never writes the
           shared state file (which is sync-replicated between nodes), and never
           counts a failure. It still probes the relay so a real outage stays
           visible; a failed probe *is* a real failure.
  unknown  no channel config or no readable node key → posts nothing and says so
           (the watchdog alerts once on this state instead of 110 times).

A dead channel and an idle non-writer node are different facts and must not
look the same in the health file.

Usage:
  decisions_digest.py [--dry-run] [--no-post] [--escalate-hours N]

Env seams (production defaults; used by tests):
  DECISIONS_STATE_FILE, DECISIONS_HEALTH_FILE, DECISIONS_RELAY,
  DECISIONS_POST_TIMEOUT, DECISIONS_NSEC_FILE, DECISIONS_OPS_CFG
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

HOME = Path.home()
BOARDS_DIR = Path(os.environ.get("HERMES_KANBAN_BOARDS_DIR",
                                 str(HOME / ".hermes/kanban/boards")))
CHANNEL_CFG = HOME / ".hermes/bot/decisions_channel.json"
STATE = Path(os.environ.get("DECISIONS_STATE_FILE",
                            str(HOME / ".hermes/state/decisions_seen.json")))
HEALTH = Path(os.environ.get("DECISIONS_HEALTH_FILE",
                             str(HOME / ".hermes/state/decisions_post_health.json")))
RELAY_OVERRIDE = os.environ.get("DECISIONS_RELAY") or None
POST_TIMEOUT = int(os.environ.get("DECISIONS_POST_TIMEOUT", "45"))
NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                        "/usr/local/bin/nak", "/usr/bin/nak"]
            if Path(c).exists()), "nak")

# this node's ops config (same file every other fleet script reads)
OPS_CFG = Path(os.environ.get("DECISIONS_OPS_CFG",
                              str(HOME / ".hermes/bot/hermes_ops.json")))
NSEC_OVERRIDE = os.environ.get("DECISIONS_NSEC_FILE") or None
# pre-2026-09-27 hardcoded identity: cobrador's key, correct on cobrador only
LEGACY_NSEC = "~/.hermes/keys/hermes-ops/cobrador.nsec"

REASON_KINDS = ("blocked", "block_loop_detected", "dependency_wait")
OPERATOR_STATUSES = ("blocked", "triage")
ESCALATE_HOURS_DEFAULT = 6

# shared classifier (single source of truth for "needs a human")
sys.path.insert(0, str(HOME / ".hermes/profiles/manager/scripts"))
try:
    import kanban_blocked_lib as _kbl  # type: ignore
except Exception:  # pragma: no cover
    _kbl = None


def log(*p) -> None:
    print("[decisions]", *p, flush=True)


def did(*parts: str) -> str:
    return "D-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def run(args: list[str], timeout: int = 45) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------
def _latest_reason(db, tid: str) -> str:
    kinds = ",".join("?" * len(REASON_KINDS))
    try:
        rows = db.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            f"AND kind IN ({kinds}) ORDER BY created_at DESC, id DESC LIMIT 10",
            (tid, *REASON_KINDS)).fetchall()
    except Exception:
        return ""
    for (payload,) in rows:
        if not payload:
            continue
        try:
            d = json.loads(payload)
            reason = d.get("reason", "") if isinstance(d, dict) else str(payload)
        except Exception:
            reason = str(payload)
        if reason:
            return reason
    return ""


def _is_operator(reason: str) -> bool:
    if _kbl is not None:
        try:
            v = _kbl.classify(reason or "", None, "blocked")
            return v.get("bucket") == _kbl.BUCKET_OPERATOR
        except Exception:
            pass
    return (reason or "").strip().lower().startswith("operator-action")


def collect_boards() -> list[dict]:
    items: list[dict] = []
    if not BOARDS_DIR.exists():
        return items
    for d in sorted(BOARDS_DIR.iterdir()):
        db_path = d / "kanban.db"
        if not db_path.exists() or d.name.startswith("_"):
            continue
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cols = {c[1] for c in db.execute("PRAGMA table_info(tasks)")}
            kind_sel = "block_kind" if "block_kind" in cols else "NULL"
            ph = ",".join("?" * len(OPERATOR_STATUSES))
            rows = db.execute(
                f"SELECT id,title,{kind_sel},status FROM tasks "
                f"WHERE status IN ({ph})", OPERATOR_STATUSES).fetchall()
            for tid, title, _kind, status in rows:
                reason = _latest_reason(db, tid)
                if not reason:
                    continue
                low = reason.strip().lower()
                base = {"board": d.name, "task": tid, "title": title or ""
                        or reason[:80]}
                if low.startswith("superseded"):
                    base.update(kind="superseded", priority="P2",
                                why=reason.split("\n", 1)[0][:200],
                                recommend="archive",
                                reversible="yes")
                    items.append(base)
                elif _is_operator(reason):
                    base.update(kind="operator-action", priority="P1",
                                why=reason.split("\n", 1)[0][:200],
                                recommend="needs you",
                                reversible="no")
                    items.append(base)
            db.close()
        except Exception:
            continue
    return items


def collect_prs() -> list[dict]:
    items: list[dict] = []
    raw = run(["gh", "search", "prs", "--author", "felixfelix-bot",
               "--state", "open", "--limit", "50",
               "--json", "repository,number,title,url,isDraft"])
    try:
        prs = json.loads(raw)
    except Exception:
        return items
    for pr in prs:
        if pr.get("isDraft"):
            continue
        repo = (pr.get("repository") or {}).get("nameWithOwner", "")
        num = pr.get("number")
        detail = run(["gh", "pr", "view", str(num), "-R", repo,
                      "--json", "reviewDecision,mergeable"])
        try:
            dd = json.loads(detail) if detail else {}
        except Exception:
            dd = {}
        review = dd.get("reviewDecision") or ""
        mergeable = dd.get("mergeable") or ""
        if review == "APPROVED" and mergeable == "MERGEABLE":
            kind, prio, rec = "ready-for-merge", "P1", "merge"
        elif review == "CHANGES_REQUESTED":
            kind, prio, rec = "pr-changes-requested", "P1", "action needed"
        else:
            continue
        items.append({"kind": kind, "priority": prio, "repo": repo,
                      "pr": num, "title": pr.get("title", "")[:100],
                      "url": pr.get("url", ""), "why": f"review={review} "
                      f"mergeable={mergeable}", "recommend": rec,
                      "reversible": "yes" if kind == "ready-for-merge" else "no"})
    return items


def collect_inbound() -> list[dict]:
    items: list[dict] = []
    db_path = BOARDS_DIR / "inbound" / "kanban.db"
    if not db_path.exists():
        return items
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for tid, title in db.execute(
                "SELECT id,title FROM tasks WHERE status NOT IN ('done','archived','cancelled')"):
            items.append({"kind": "inbound", "priority": "P2", "board": "inbound",
                          "task": tid, "title": title or "", "why": "external card needs routing",
                          "recommend": "route", "reversible": "yes"})
        db.close()
    except Exception:
        pass
    return items


REVIEW_DECISIONS = HOME / ".hermes/state/review_decisions.jsonl"


def collect_review_decisions() -> list[dict]:
    """Operator decisions emitted by review_fix_emit (A/B forks from a review).

    These never become block-hung cards: they surface here with their options so
    the operator can answer, and the responder can promote the fix.
    """
    items: list[dict] = []
    try:
        lines = REVIEW_DECISIONS.read_text().splitlines()
    except Exception:
        return items
    seen: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        track = d.get("track", "?")
        uniq = f"{d.get('repo')}#{d.get('pr')}:{track}"
        if uniq in seen:
            continue
        seen.add(uniq)
        opts = d.get("options") or []
        items.append({
            "kind": f"review-decision-{track}",
            "priority": "P1",
            "board": d.get("board"),
            "repo": d.get("repo", ""),
            "pr": d.get("pr"),
            "title": (d.get("summary") or "")[:100],
            "url": d.get("review_url", ""),
            "why": "review finding needs an operator decision",
            "recommend": " | ".join(opts)[:200] if opts else "decide",
            "reversible": "no",
            "options": opts,
        })
    return items


# ---------------------------------------------------------------------------
# rendering + posting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# rendering + posting
# ---------------------------------------------------------------------------
def key_of(it: dict) -> str:
    if "pr" in it:
        return did("pr", it["repo"], str(it["pr"]), it["kind"])
    return did(it["board"], it["task"], it["kind"])


def render(it: dict) -> str:
    kid = key_of(it)
    src = it.get("url") or f"{it.get('board','')}/{it.get('task','')}"
    lines = [
        f"[{kid} · {it['priority']}] {it['kind']}",
        f"What: {it.get('title','')}",
        f"Why: {it.get('why','')}",
        f"Source: {src}",
        f"Recommend: {it.get('recommend','')} · reversible: {it.get('reversible','?')}",
        f"Reply: {it.get('recommend','')} {kid}",
    ]
    return "\n".join(lines)


def channel() -> dict | None:
    try:
        return json.loads(CHANNEL_CFG.read_text())
    except Exception:
        return None


def nsec_path() -> Path:
    """This node's identity file: env → ops config `node_nsec` → legacy path."""
    if NSEC_OVERRIDE:
        return Path(os.path.expanduser(NSEC_OVERRIDE))
    try:
        p = json.loads(OPS_CFG.read_text()).get("node_nsec")
    except Exception:
        p = None
    return Path(os.path.expanduser(p or LEGACY_NSEC))


def _bridge_sec() -> str | None:
    try:
        return nsec_path().read_text().strip()
    except OSError:
        return None


def _pubkey_of(sec: str) -> str:
    """Derive the hex pubkey for a secret key. Never logs the secret."""
    try:
        return subprocess.run([NAK, "key", "public", sec], capture_output=True,
                              text=True, timeout=15).stdout.strip().lower()
    except Exception:
        return ""


def channel_role() -> tuple[str, str]:
    """Return (role, detail) where role is writer | observer | unknown.

    The channel config already lists its members, so membership is decidable
    locally and offline: a node whose identity is not a member cannot post to a
    `closed`, `restricted_writes` NIP-29 group, and saying so is not an outage.
    """
    cfg = channel()
    if not cfg:
        return "unknown", f"no channel config ({CHANNEL_CFG})"
    members = {str(m).strip().lower() for m in (cfg.get("members") or [])}
    sec = _bridge_sec()
    if not sec:
        return "unknown", f"node nsec unreadable ({nsec_path()})"
    if not members:
        return "writer", "channel config lists no members; assuming writer"
    pub = _pubkey_of(sec).strip().lower()
    if not pub:
        return "unknown", "could not derive this node's pubkey"
    name = cfg.get("name", "channel")
    if pub in members:
        return "writer", f"identity {pub[:16]}… is a {name} member"
    return "observer", (f"identity {pub[:16]}… is not a member of {name} — "
                        f"this node does not post")


def _out_tail(out: str, n: int = 160) -> str:
    return re.sub(r"\s+", " ", (out or "").strip())[-n:]


def post(text: str, cfg: dict | None, relay: str) -> tuple[bool, str]:
    """Publish one kind-9 message. Returns (ok, raw_output_tail)."""
    if not cfg:
        return False, "no decisions_channel.json; cannot post"
    sec = _bridge_sec()
    if not sec:
        return False, "bridge nsec unreadable (~/.hermes/keys/hermes-ops/cobrador.nsec)"
    r = subprocess.run(
        [NAK, "event", "-k", "9", "-t", f"h={cfg['orange_group']}", "-c", text,
         "--auth", "--sec", sec, relay],
        capture_output=True, text=True, timeout=POST_TIMEOUT)
    both = (r.stdout or "") + (r.stderr or "")
    ok = "success" in both
    return ok, _out_tail(both)


def probe(cfg: dict | None, relay: str) -> tuple[str, str]:
    """Cheap liveness probe used when there is nothing to post.

    Returns ("ok"|"down"|"unknown", detail). Any explicit connect/auth failure
    is "down"; a successful NIP-42 AUTH handshake with no failure markers is
    "ok" (a relay with zero events must NOT read as an outage)."""
    if not cfg:
        return "down", "no decisions_channel.json"
    sec = _bridge_sec()
    if not sec:
        return "down", "bridge nsec unreadable"
    try:
        r = subprocess.run(
            [NAK, "req", "-k", "9", "-t", f"h={cfg['orange_group']}", "-l", "1",
             "--auth", "--sec", sec, relay],
            capture_output=True, text=True, timeout=POST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "unknown", "probe timed out"
    both = ((r.stdout or "") + (r.stderr or ""))
    low = both.lower()
    for marker in ("failed", "refused", "unreachable", "closed: auth-required",
                   "connection took too long", "timed out"):
        if marker in low:
            return "down", _out_tail(both)
    if "authenticating" in low:
        return "ok", _out_tail(both)
    return "unknown", _out_tail(both)


# ---------------------------------------------------------------------------
# health (machine-readable alert channel for watchdogs)
# ---------------------------------------------------------------------------
def load_health() -> dict:
    try:
        return json.loads(HEALTH.read_text())
    except Exception:
        return {}


def save_health(now: int, posted: int, total: int, failures: int,
                last_error: str, probe_state: str,
                role: str = "writer") -> tuple[dict, int]:
    h = load_health()
    prev_failures = int(h.get("consecutive_failures", 0) or 0)
    if role == "observer":
        # not this node's job: never inflate the failure counter. An observer
        # that reported failures is exactly how a healthy channel came to look
        # dead for 110 consecutive ticks.
        h["consecutive_failures"] = 0
        h["last_error"] = ""
        h.pop("last_failure_at", None)
    elif failures:
        h["consecutive_failures"] = prev_failures + 1
        h["last_error"] = last_error[:300]
        h["last_failure_at"] = now
    else:
        h["consecutive_failures"] = 0
        h["last_success"] = now
    h.update(role=role, last_attempt=now, posted_last=posted,
             to_post_last=total, probe=probe_state,
             relay=RELAY_OVERRIDE or (channel() or {}).get("relay", ""))
    try:
        HEALTH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=1))
        tmp.replace(HEALTH)
    except OSError:
        pass
    return h, prev_failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--escalate-hours", type=int, default=ESCALATE_HOURS_DEFAULT)
    args = ap.parse_args()

    cfg = channel()
    relay = RELAY_OVERRIDE or (cfg or {}).get("relay", "")
    now = int(time.time())

    # ---- role gate: only a channel member may post --------------------------
    # A node whose identity is not a member of this closed NIP-29 group cannot
    # deliver, and must not describe that as a channel outage. Observers skip
    # collection entirely (cheap) and never touch the sync-replicated state file.
    role, role_detail = channel_role()
    if role != "writer":
        log(f"role={role} — {role_detail}")
        failures = 0
        last_error = ""
        probe_state = "n/a"
        if role == "observer":
            probe_state, detail = probe(cfg, relay)
            if probe_state == "down":
                failures = 1
                last_error = detail
                log(f"ALERT relay-down: {relay} probe failed — {detail}")
        log("posted 0/0 (observer node: not a decisions writer)")
        h, prev_failures = save_health(now, 0, 0, failures, last_error,
                                       probe_state, role=role)
        if failures:
            log(f"ALERT decision-channel-degraded: {failures} failure(s) this "
                f"tick at {relay} — consecutive_failures="
                f"{h.get('consecutive_failures')} last_error={last_error[:120]}")
        elif prev_failures > 0:
            log(f"recovered: decision channel OK again after {prev_failures} "
                f"failing tick(s) (relay={relay}, probe={probe_state})")
        return 0

    items = (collect_boards() + collect_prs() + collect_inbound()

             + collect_review_decisions())
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    seen: dict[str, dict] = state.get("items", {})

    # ---- classify (nothing is written yet) ---------------------------------
    queue: list[tuple[str, str, dict]] = []   # (key, text, deferred update)
    for it in items:
        kid = key_of(it)
        sig = json.dumps({k: it.get(k) for k in
                          ("kind", "priority", "why", "recommend", "title")},
                         sort_keys=True)
        rec = seen.get(kid)
        if rec is not None and rec.get("snooze_until", 0) > now:
            continue  # operator snoozed this item
        if rec is None or rec.get("sig") is None:
            # never delivered (new item, or a first delivery that failed)
            queue.append((kid, render(it),
                          {"kind": "new", "sig": sig, "item": it}))
        elif rec.get("sig") != sig:
            queue.append((kid, "[UPDATED]\n" + render(it),
                          {"kind": "update", "sig": sig, "item": it}))
        else:
            age_h = (now - rec.get("first_seen", now)) / 3600
            last = rec.get("last_posted", now)
            if age_h >= args.escalate_hours and now - last >= args.escalate_hours * 3600:
                queue.append((kid, f"[AGED {age_h:.1f}h] " + render(it),
                              {"kind": "aged", "sig": sig}))

    log(f"collected={len(items)} to_post={len(queue)} tracked={len(seen)}")
    if args.dry_run or args.no_post:
        for _kid, card, _upd in queue:
            print("-----")
            print(card)
        return 0

    # items that vanished from every source -> resolved (delivered on success)
    present = {key_of(it) for it in items}
    for kid, rec in list(seen.items()):
        if kid not in present and rec.get("status") == "open":
            queue.append((kid, f"[RESOLVED] {kid}",
                          {"kind": "resolved", "sig": rec.get("sig")}))

    # ---- deliver: state advances ONLY on a successful post ------------------
    posted = failed = 0
    last_error = ""
    for kid, card, upd in queue:
        ok, out = post(card, cfg, relay)
        rec = seen.setdefault(kid, {"status": "open", "first_seen": now,
                                    "sig": None})
        if ok:
            posted += 1
            if upd["kind"] == "resolved":
                rec.update(status="resolved", resolved_at=now)
            else:
                rec.update(sig=upd["sig"], last_posted=now,
                           item=upd.get("item", rec.get("item")))
            rec.pop("last_post_error", None)
            rec.pop("unsent_attempts", None)
            rec.pop("last_attempt", None)
        else:
            failed += 1
            last_error = out
            # keep sig=None / old sig so the next tick retries this item
            rec["unsent_attempts"] = int(rec.get("unsent_attempts", 0) or 0) + 1
            rec["last_post_error"] = out
            rec["last_attempt"] = now
            log(f"post failed: {out}")
        time.sleep(0.5)

    # persist state AFTER delivery: only successfully posted items are stamped
    STATE.parent.mkdir(parents=True, exist_ok=True)
    stmp = STATE.with_suffix(".json.tmp")
    stmp.write_text(json.dumps({"items": seen, "ts": now}, indent=1))
    stmp.replace(STATE)

    probe_state = "n/a"
    if not queue:
        # nothing to deliver: prove the channel is alive so a relay outage with
        # an empty queue is still visible (17 failures once hid behind this).
        probe_state, detail = probe(cfg, relay)
        if probe_state == "down":
            failed += 1
            last_error = detail
            log(f"ALERT relay-down: {relay} probe failed — {detail}")

    log(f"posted {posted}/{len(queue)}")
    h, prev_failures = save_health(now, posted, len(queue), failed, last_error,
                                   probe_state)

    if failed:
        log(f"ALERT decision-channel-degraded: {failed} failure(s) this tick at "
            f"{relay} — consecutive_failures={h.get('consecutive_failures')} "
            f"last_error={last_error[:120]}")
    elif prev_failures > 0:
        log(f"recovered: decision channel OK again after {prev_failures} failing "
            f"tick(s) (relay={relay}, probe={probe_state})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
