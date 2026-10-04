#!/usr/bin/env python3
"""fleet_queue.py — pull-based, fleet-aware work queue core (K.2–K.4).

- classify()  : score a task's resource class + fit tags/requires/exclusions
- fit_ok()    : can this node run it?
- headroom()  : normalized spare-capacity score (imported from fleet_health)
- route()     : claim | defer | skip — "own resource gates" policy
- resolve_claims() : deterministic lease winner (earliest ts, hostname tie-break)
- signed buzz I/O for fleet-task / fleet-claim / fleet-done events

Local durable state: ~/.hermes/bot/fleet_queue_state.json

Pure functions are unit-tested (test_fleet_queue.py). CLI:
  fleet_queue.py advertise --board B --task T [--repo R] [--title S] [--class C]
  fleet_queue.py ingest
  fleet_queue.py sweep            # ingest + route + publish claims
  fleet_queue.py status
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
STATE = BOT / "fleet_queue_state.json"
FLEET_CFG = BOT / "fleet.json"
FIT = BOT / "fleet_fit.json"
OPS = BOT / "hermes_ops.json"

CLASS_RANK = {"light": 0, "medium": 1, "heavy": 2}
HEAVY_MARGIN = 0.15     # a heavy task needs this much more headroom elsewhere
FALLBACK_S = 1800       # after this, heavy may be claimed by any fitting node
HEADROOM_FLOOR = 0.05   # below this a node has effectively no headroom
DEFER_MARGIN = 0.15     # light/medium defer to a peer this much more idle (D-130)
PEER_MAX_AGE_S = 300    # ignore peer health older than this (staleness guard, §14.21)

_HEAVY_KW = ("build", "compile", "test suite", "full test", "e2e", "playwright",
             "matrix", "benchmark", "cargo", "platformio", "cross-compile",
             "integration test", "long-running", "migration", "fuzz")
_LIGHT_KW = ("docs", "readme", "typo", "rename", "changelog", "llms.txt",
             "status update", "comment")
# D-130: coding work runs on the heavy model tier (glm-5.3)
_CODING_KW = ("code", "implement", "refactor", "fix", "bug", "pytest", "compile",
              "migrate", "schema", "parser", "api", "function", "class",
              "module", "script", "endpoint", "query", "algorithm", "review")

# Phase V6.1 — task disk-footprint estimate. Explicit `disk:<n>` tag wins, then a
# keyword heuristic, then a per-class default. Placement compares this against a
# node's free disk minus its reserve (V6.3).
_DISK_CLASS_DEFAULT = {"light": 1.0, "medium": 5.0, "heavy": 15.0}
_DISK_HEAVY_GB = 10.0
_DISK_KW = (
    ("openwrt sdk", 25), ("android-sdk", 15), ("android sdk", 15),
    ("docker build", 10), ("docker pull", 10), ("model download", 20),
    ("huggingface", 20), ("compileall", 3), ("bazel", 10), ("cargo build", 8),
    ("npm ci", 3), ("conda", 8), ("node_modules", 3), ("test suite", 3),
    ("e2e", 3), ("build", 5), ("compile", 5),
)


def _read_json(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d if d is not None else {}


def _write_json(p: Path, payload):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(p)


def _self() -> str:
    return _read_json(FLEET_CFG, {}).get("node") or socket.gethostname()


def load_peers(now: float | None = None) -> list[dict]:
    """Fresh peer health dicts, deduped by node (prefer health over heartbeat).

    Staleness guard (§14.21): a peer whose ``ts`` is older than
    ``PEER_MAX_AGE_S`` is dropped, so routing/balancing never acts on a dead or
    lagging node's stale telemetry. ``peers/<node>.health.json`` (carries
    ``headroom_score``) wins over the raw ``peers/<node>.json`` heartbeat; ties
    prefer the newer ``ts``.
    """
    now = time.time() if now is None else now

    def _rank(h: dict) -> tuple[int, float]:
        return (1 if "headroom_score" in h else 0, float(h.get("ts", 0) or 0))

    best: dict[str, dict] = {}
    for f in sorted((BOT / "peers").glob("*.json")):
        h = _read_json(f, None)
        if not isinstance(h, dict):
            continue
        n = h.get("node")
        if not n or now - float(h.get("ts", 0) or 0) > PEER_MAX_AGE_S:
            continue
        if n not in best or _rank(h) > _rank(best[n]):
            best[n] = h
    return list(best.values())


def _fit() -> dict:
    return _read_json(FIT, {})


# ── pure logic ────────────────────────────────────────────────────────────────

def classify(board: str, repo: str, title: str, body: str,
             explicit_tags: str = "", explicit_class: str = "") -> dict:
    """Return {resource_class, tags, requires, exclusions, repo}."""
    text = f"{title} {body}".lower()
    cls = explicit_class if explicit_class in CLASS_RANK else ""
    if not cls:
        if any(k in text for k in _HEAVY_KW):
            cls = "heavy"
        elif any(k in text for k in _LIGHT_KW):
            cls = "light"
        else:
            cls = "medium"
    tags = [t.strip() for t in explicit_tags.split(",") if t.strip()]
    if repo and f"repo:{repo}" not in tags:
        tags.append(f"repo:{repo}")
    requires = [t for t in tags
                if t.startswith(("repo:", "cap:", "tool:"))]
    exclusions = [t[len("exclude:"):] for t in tags if t.startswith("exclude:")]
    exclusions += [t for t in tags if t == "canary-only"]
    disk_gb_est = estimate_disk_gb(text, cls, tags)
    return {"resource_class": cls, "tags": tags, "requires": requires,
            "exclusions": exclusions, "repo": repo,
            "disk_gb_est": disk_gb_est, "disk_heavy": disk_gb_est >= _DISK_HEAVY_GB}


def _class_rank(c: str) -> int:
    return CLASS_RANK.get(c, 1)


def estimate_disk_gb(text: str, cls: str, tags: list[str]) -> float:
    """Task disk estimate (GB): explicit `disk:<n>` tag > keyword > class default."""
    for t in tags:
        if t.startswith("disk:"):
            try:
                return float(t.split(":", 1)[1])
            except ValueError:
                pass
    low = (text or "").lower()
    for kw, gb in _DISK_KW:
        if kw in low:
            return float(gb)
    return float(_DISK_CLASS_DEFAULT.get(cls, 5.0))


def disk_fit(node_health: dict, task: dict, reserve_gb: float = 0.0) -> bool:
    """True if the node can host the task's disk footprint after its reserve
    (Phase V6.3). `node_health.disk_reserve_gb` overrides `reserve_gb`."""
    reserve = float(node_health.get("disk_reserve_gb", reserve_gb) or reserve_gb)
    free = float(node_health.get("disk_free_gb") or 0)
    est = float(task.get("disk_gb_est") or 0)
    return (free - reserve) >= est


def disk_defer(me: dict, peers: list[dict], task: dict,
               reserve_gb: float = 0.0) -> str | None:
    """If `me` lacks disk for the task, name the fitting peer with the most free
    disk (post-reserve); else None (Phase V6.3)."""
    if disk_fit(me, task, reserve_gb):
        return None
    best, best_name = -1.0, None
    for p in peers or []:
        if p.get("node") == me.get("node") or not disk_fit(p, task, reserve_gb):
            continue
        free = float(p.get("disk_free_gb") or 0)
        if free > best:
            best, best_name = free, p.get("node")
    return best_name


def disk_claim_warning(node_health: dict, task: dict,
                       reserve_gb: float = 0.0) -> str | None:
    """Warn-only (until V5.3 completes): a disk-heavy task about to run where it
    doesn't fit after reserve. Returns a message, or None."""
    if float(task.get("disk_gb_est") or 0) >= _DISK_HEAVY_GB and \
            not disk_fit(node_health, task, reserve_gb):
        return (f"disk-heavy {task.get('id') or task.get('title')} on "
                f"{node_health.get('node')} needs ~{task.get('disk_gb_est')}G, "
                f"free {node_health.get('disk_free_gb')}G - reserve {reserve_gb}G "
                f"(warn-only until V5.3)")
    return None


def is_heavy_or_coding(task: dict) -> bool:
    """D-130: route to the heavy model tier (glm-5.3) when the task is heavy or
    reads as coding; light/docs stay on the node default (glm-5.2)."""
    if task.get("resource_class") == "heavy":
        return True
    text = f"{task.get('title', '')} {task.get('body', '')}".lower()
    return any(k in text for k in _HEAVY_KW + _CODING_KW)


def reviewer_profile(task: dict) -> str | None:
    """D-131: honour a `reviewer:<name>` (or `review:<name>`) marker so a review
    card runs with the right cross-family profile on whichever node claims it."""
    import re
    tags = task.get("tags") or []
    for t in tags:
        m = re.match(r"^(?:reviewer|review):([a-z0-9_-]+)$", str(t).lower())
        if m:
            return f"worker-reviewer-{m.group(1)}"
    text = f"{task.get('title', '')} {task.get('body', '')}"
    m = re.search(r"\breviewer[:=]\s*([a-z0-9_-]+)", text, re.I)
    if m:
        return f"worker-reviewer-{m.group(1).lower()}"
    return None


def fit_ok(fit: dict, task: dict) -> tuple[bool, str]:
    """Can a node with this fit profile run the task?"""
    if not fit:
        return True, "no-fit-profile"
    repos = set(fit.get("repos", []))
    caps = set(fit.get("capabilities", []))
    excl = set(fit.get("exclusions", []))
    if excl & set(task.get("exclusions", [])):
        return False, f"excluded:{sorted(excl & set(task.get('exclusions', [])))}"
    for r in task.get("requires", []):
        if r.startswith("repo:") and r[5:] not in repos:
            return False, f"missing {r}"
        if r.startswith("cap:") and r[4:] not in caps:
            return False, f"missing {r}"
    if _class_rank(task.get("resource_class", "medium")) > \
            _class_rank(fit.get("max_class", "heavy")):
        return False, "class>max_class"
    return True, ""


def headroom(health: dict) -> float:
    if "headroom_score" in health:
        try:
            return float(health["headroom_score"])
        except (TypeError, ValueError):
            pass
    try:
        from fleet_health import headroom_score  # type: ignore
        return float(headroom_score(health))
    except Exception:
        return 0.0


def route(task: dict, my_health: dict, peer_healths: list[dict],
          my_fit: dict, now: float) -> tuple[str, str]:
    """Decide whether THIS node should claim a task. Returns (decision, reason).

    claim = I take it; defer = leave for a better-fitting/higher-headroom peer;
    skip = I cannot/should not run it.
    """
    ok, why = fit_ok(my_fit, task)
    if not ok:
        return "skip", f"unfit: {why}"
    mine = headroom(my_health)
    cls = task.get("resource_class", "medium")

    # Phase V6.3 — disk-aware: a task whose footprint doesn't fit here (after the
    # node's reserve) must defer to the fitting peer with the most free disk.
    dpeer = disk_defer(my_health, peer_healths, task,
                       float((my_fit or {}).get("disk_reserve_gb") or 0.0))
    if dpeer:
        return "defer", (f"disk {task.get('disk_gb_est')}G->{dpeer} "
                         f"(free {my_health.get('disk_free_gb')}G < need)")

    best_peer, best_name = -1.0, None
    for p in peer_healths or []:
        if p.get("node") == my_health.get("node"):
            continue
        f = p.get("fit") or {}
        okp, _ = fit_ok(f, task) if f else (False, "no-fit")
        if not okp:
            continue
        hp = headroom(p)
        if hp > best_peer:
            best_peer, best_name = hp, p.get("node")

    cts = task.get("created_ts")
    waited = (now - float(cts)) if cts is not None else 0.0

    if cls == "heavy":
        if best_name and best_peer >= mine + HEAVY_MARGIN and waited < FALLBACK_S:
            return "defer", f"heavy->{best_name}({best_peer:.2f}>{mine:.2f}+{HEAVY_MARGIN})"
        if mine >= HEADROOM_FLOOR:
            return "claim", f"heavy headroom {mine:.2f}"
        if best_name:
            return "defer", f"heavy no-local-headroom->{best_name}"
        return "skip", f"heavy no-headroom (waited {int(waited)}s)"

    # light / medium — defer to a materially more idle peer (D-130)
    if best_name and best_peer >= HEADROOM_FLOOR and \
            best_peer >= mine + DEFER_MARGIN:
        return "defer", (f"{cls}->{best_name}"
                         f"({best_peer:.2f}>={mine:.2f}+{DEFER_MARGIN})")
    if mine >= HEADROOM_FLOOR:
        return "claim", f"{cls} headroom {mine:.2f}"
    if best_name and best_peer >= HEADROOM_FLOOR:
        return "defer", f"{cls}->{best_name}({best_peer:.2f})"
    return "skip", f"{cls} no-headroom"


def resolve_claims(claims: dict) -> dict:
    """claims: {task_id: [{node, ts, headroom, started}]} -> {task_id: winner}.

    D-130 resource-aware lease with a **started-sticky** guard: once a node has
    begun executing a task (`started`), it keeps the lease (no preemption of
    running work, no-kill). Otherwise prefer highest current headroom; tie-break
    earliest ts, then node. Deterministic on identical inputs. Backward
    compatible when `headroom`/`started` are absent (earliest ts)."""
    winners = {}
    for tid, entries in claims.items():
        valid = [e for e in entries if e.get("node") and e.get("ts") is not None]
        if not valid:
            continue
        started = {e["node"] for e in valid if e.get("started")}
        if started:
            pool = [e for e in valid if e["node"] in started]
            pool.sort(key=lambda e: (float(e["ts"]), e["node"]))
        else:
            pool = valid
            pool.sort(key=lambda e: (-float(e.get("headroom") or 0.0),
                                     float(e["ts"]), e["node"]))
        winners[tid] = pool[0]["node"]
    return winners


def should_yield(task: dict, my_health: dict, peer_healths: list[dict],
                 my_fit: dict, now: float,
                 margin: float = DEFER_MARGIN) -> str | None:
    """D-130 rebalance: return the name of a materially more-idle fitting peer
    that should take this task, else None. Callers MUST only use this for
    tasks that are queued/claimed but NOT running (no-kill policy)."""
    mine = headroom(my_health)
    me = my_health.get("node")
    best, best_name = mine + margin, None
    for p in peer_healths or []:
        if p.get("node") == me:
            continue
        f = p.get("fit") or {}
        okp, _ = fit_ok(f, task) if f else (True, "")
        if not okp:
            continue
        hp = headroom(p)
        if hp >= best:
            best, best_name = hp, p.get("node")
    return best_name


# ── signed buzz I/O ───────────────────────────────────────────────────────────

def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _ops() -> dict:
    return _read_json(OPS, {})


def publish(content: dict, tag: str) -> bool:
    o = _ops()
    group = o.get("orange_group")
    nsec = Path(os.path.expanduser(o.get("node_nsec", "")))
    if not group or not nsec.exists():
        return False
    try:
        key = nsec.read_text().strip()
    except OSError:
        return False
    args = [_nak(), "event", "-k", "9",
            "-c", json.dumps(content, separators=(",", ":")),
            "-t", f"h={group}", "-t", "client=hermes-fleet", "-t", f"t={tag}",
            "--sec", key, "--auth", o.get("orange_relay", "wss://relay.orangesync.tech")]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=45)
        return "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def fetch(tag: str, limit: int = 200) -> list[dict]:
    o = _ops()
    group = o.get("orange_group")
    nsec = Path(os.path.expanduser(o.get("node_nsec", "")))
    if not group or not nsec.exists():
        return []
    try:
        key = nsec.read_text().strip()
    except OSError:
        return []
    args = [_nak(), "req", "--auth", "--sec", key, "-k", "9",
            "-t", f"h={group}", "-t", f"t={tag}", "--limit", str(limit),
            o.get("orange_relay", "wss://relay.orangesync.tech")]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
            body = json.loads(ev.get("content") or "{}")
            body["_ts"] = float(ev.get("created_at", 0) or 0)
            out.append(body)
        except Exception:
            continue
    return out


# ── private bus (SSH peer-push ledger; never the public relay) ─────────────────

PRIVATE_DIR = HERMES / "state" / "fleet-private"


def _private_paths() -> list[Path]:
    paths = list(PRIVATE_DIR.glob("events-*.jsonl")) if PRIVATE_DIR.exists() else []
    paths += list((BOT / "peers").glob("events-*.jsonl"))
    return paths


def publish_private(content: dict, tag: str) -> bool:
    """Append a coordination event to this node's private ledger. The heartbeat
    ships the ledger to the peer over SSH. Never touches the public relay."""
    try:
        PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"tag": tag, "ts": time.time(), "content": content},
                          separators=(",", ":"))
        with (PRIVATE_DIR / f"events-{_self()}.jsonl").open("a") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False


def fetch_private(tag: str, limit: int = 500) -> list[dict]:
    out = []
    for p in _private_paths():
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("tag") != tag:
                    continue
                body = rec.get("content") or {}
                body["_ts"] = rec.get("ts", 0)
                out.append(body)
        except OSError:
            continue
    return out[-limit:]


def publish_any(content: dict, tag: str, private: bool) -> bool:
    return publish_private(content, tag) if private else publish(content, tag)


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_advertise(args) -> int:
    task = classify(args.board, args.repo, args.title or "", args.body or "",
                    explicit_tags=args.tags or "", explicit_class=args.cls or "")
    node = _self()
    payload = {
        "type": "fleet-task", "id": args.task, "board": args.board,
        "repo": args.repo, "title": args.title or args.task,
        "body": (args.body or "")[:8000],
        "resource_class": task["resource_class"], "requires": task["requires"],
        "exclusions": task["exclusions"], "tags": task["tags"],
        "actor": node, "created_ts": time.time(),
    }
    ok = publish(payload, "fleet-task")
    print(f"advertise {args.task} class={task['resource_class']} "
          f"requires={task['requires']} exclusions={task['exclusions']} pushed={ok}")
    return 0 if ok else 1


def _ingest_into(state: dict) -> None:
    tasks = state.setdefault("tasks", {})
    claims = state.setdefault("claims", {})
    def _all(tag: str) -> list[dict]:
        return fetch(tag) + fetch_private(tag)
    for t in _all("fleet-task"):
        tid = t.get("id")
        if not tid:
            continue
        cur = tasks.get(tid)
        if cur is None or float(t.get("_ts", 0)) >= float(cur.get("_ts", 0)):
            t.pop("_ts", None)
            tasks[tid] = t
    for c in _all("fleet-claim"):
        tid = c.get("id") or c.get("task")
        if not tid:
            continue
        node = c.get("actor")
        ts = c.get("claim_ts", c.get("_ts"))
        # dedupe: one entry per (node, ts) — kills the claim storm
        entries = claims.setdefault(tid, [])
        if not any(e.get("node") == node and e.get("ts") == ts for e in entries):
            entries.append({"node": node, "ts": ts})
    for d in _all("fleet-done"):
        tid = d.get("id")
        if tid and tid in tasks:
            tasks[tid]["done"] = True
    # fleet-running: liveness heartbeat from whichever node actually started a
    # task. Lets a holding node tell "slow to start" from "never started" and
    # time out a dead peer's hold (D-128 follow-up: AV-FIX-1 offload freeze).
    for r in _all("fleet-running"):
        tid = r.get("id") or r.get("task")
        if not tid:
            continue
        ts = float(r.get("ts") or r.get("_ts") or 0)
        runs = state.setdefault("running", {})
        prev = runs.get(tid)
        if prev is None or ts >= float(prev.get("ts", 0)):
            runs[tid] = {"node": r.get("actor"), "ts": ts}


def cmd_ingest(args) -> int:
    state = _read_json(STATE, {"tasks": {}, "claims": {}})
    _ingest_into(state)
    _write_json(STATE, state)
    print(f"ingested: tasks={len(state.get('tasks', {}))} "
          f"claims={len(state.get('claims', {}))}")
    return 0


def cmd_sweep(args) -> int:
    state = _read_json(STATE, {"tasks": {}, "claims": {}})
    _ingest_into(state)
    health = _read_json(BOT / "fleet_health.json", {})
    now = time.time()
    # Staleness guard (§14.21): never route/defer to a peer whose health is old.
    peers = load_peers(now)
    me = _self()
    my_fit = _fit()
    winners = resolve_claims(state.get("claims", {}))
    claimed = []
    for tid, task in state.get("tasks", {}).items():
        if task.get("done"):
            continue
        if tid in winners:
            continue
        decision, reason = route(task, health, peers, my_fit, now)
        task["decision"] = decision
        task["decision_reason"] = reason
        if decision == "claim":
            try:
                import fleet_ownership as fo  # type: ignore
                ok, why = fo.can_start(tid, me, lease_winner=winners.get(tid))
                if not ok:
                    task["decision_reason"] = f"owned: {why}"
                    continue
            except Exception:
                pass
            payload = {"type": "fleet-claim", "id": tid, "actor": me,
                       "claim_ts": now}
            if publish(payload, "fleet-claim"):
                state.setdefault("claims", {}).setdefault(tid, []).append(
                    {"node": me, "ts": now})
                claimed.append(tid)
    _write_json(STATE, state)
    print(f"sweep: tasks={len(state.get('tasks', {}))} "
          f"won_by_peer={len([t for t, n in winners.items() if n != me])} "
          f"claimed_now={claimed}")
    return 0


def cmd_status(args) -> int:
    state = _read_json(STATE, {"tasks": {}, "claims": {}})
    winners = resolve_claims(state.get("claims", {}))
    me = _self()
    print(f"fleet queue ({me}): tasks={len(state.get('tasks', {}))}")
    for tid, t in list(state.get("tasks", {}).items())[:30]:
        w = winners.get(tid, "-")
        print(f"  {tid:16} {t.get('resource_class','?'):6} "
              f"board={t.get('board','?')} winner={w} dec={t.get('decision','-')}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet work queue")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("advertise")
    p.add_argument("--board", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--class", dest="cls", default="")
    p.set_defaults(func=cmd_advertise)

    p = sub.add_parser("ingest"); p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("sweep"); p.set_defaults(func=cmd_sweep)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
