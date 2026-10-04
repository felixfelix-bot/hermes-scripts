#!/usr/bin/env python3
"""fleet_claim.py — coordinated reply claims for the Buzz responder election
(D-129).

Before a node replies to an operator message it publishes a lightweight claim:

    {"type": "fleet-reply-claim", "id": <operator event id>,
     "actor": <node>, "headroom": <0..1>, "ts": <epoch>}

to the private OrangeSync coordination group (NIP-29 group, kind 9,
`t=fleet-reply-claim`). The winner is deterministic (see responder_election),
so both nodes agree. After replying, the winner publishes `fleet-reply-done`,
which also drives failover.

Every record is mirrored to this node's SSH private ledger
(`~/.hermes/state/fleet-private/events-<node>.jsonl`), which the heartbeat
ships to peers. Claims never touch the public board repo or ngit.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
PRIVATE_DIR = HERMES / "state" / "fleet-private"

CLAIM_TAG = "fleet-reply-claim"
DONE_TAG = "fleet-reply-done"


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _relays(c: dict) -> list[str]:
    rs = [c.get("relay")] + list(c.get("relays") or [])
    return [r for r in dict.fromkeys(rs) if r]


def _key(c: dict) -> str:
    return Path(os.path.expanduser(c["nsec_path"])).read_text().strip()


def _node(c: dict, fallback: str = "") -> str:
    try:
        return json.loads((BOT / "fleet.json").read_text()).get("node") or fallback
    except Exception:
        return fallback or os.uname().nodename


def _coord_group(c: dict) -> str:
    e = c.get("election") or {}
    return e.get("coord_group") or c.get("coord_group") or ""


def _run(args: list[str], timeout: int = 25):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


# ── private ledger (SSH-shipped; never the relay) ─────────────────────────────

def _private_append(node: str, tag: str, payload: dict) -> None:
    try:
        PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"tag": tag, "ts": time.time(),
                           "content": payload}, separators=(",", ":"))
        with (PRIVATE_DIR / f"events-{node}.jsonl").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _private_read(tag: str) -> list[dict]:
    out = []
    paths = list(PRIVATE_DIR.glob("events-*.jsonl")) if PRIVATE_DIR.exists() else []
    paths += list((BOT / "peers").glob("events-*.jsonl"))
    for p in paths:
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
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
            body.setdefault("ts", rec.get("ts", 0))
            out.append(body)
    return out


# ── relay I/O ─────────────────────────────────────────────────────────────────

def publish(c: dict, tag: str, payload: dict, node: str = "") -> bool:
    node = node or _node(c)
    group = _coord_group(c)
    _private_append(node, tag, payload)
    if not group:
        return True
    args = [_nak(), "event", "-k", "9",
            "-c", json.dumps(payload, separators=(",", ":")),
            "-t", f"h={group}", "-t", "client=hermes-fleet", "-t", f"t={tag}",
            "--sec", _key(c), "--auth", c.get("relay")]
    r = _run(args, 45)
    return bool(r and "success" in (r.stdout + r.stderr))


def fetch(c: dict, tag: str, since: float, limit: int = 300) -> list[dict]:
    out = list(_private_read(tag))
    group = _coord_group(c)
    if not group:
        return out
    args = [_nak(), "req", "--auth", "--sec", _key(c), "-k", "9",
            "-t", f"h={group}", "-t", f"t={tag}",
            "--since", str(int(since)), "--limit", str(limit), c.get("relay")]
    r = _run(args, 30)
    if r:
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
                body = json.loads(ev.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            body.setdefault("ts", ev.get("created_at", 0))
            out.append(body)
    return out


def publish_claim(c: dict, msg_id: str, headroom: float, node: str = "") -> bool:
    node = node or _node(c)
    return publish(c, CLAIM_TAG, {
        "type": CLAIM_TAG, "id": msg_id, "actor": node,
        "headroom": round(float(headroom), 3), "ts": time.time(),
    }, node=node)


def publish_done(c: dict, msg_id: str, node: str = "") -> bool:
    node = node or _node(c)
    return publish(c, DONE_TAG, {
        "type": DONE_TAG, "id": msg_id, "actor": node, "ts": time.time(),
    }, node=node)


def fetch_claims(c: dict, msg_id: str, since: float) -> list[dict]:
    return [r for r in fetch(c, CLAIM_TAG, since) if r.get("id") == msg_id]


def fetch_done(c: dict, since: float, limit: int = 300) -> set[str]:
    return {r.get("id") for r in fetch(c, DONE_TAG, since, limit) if r.get("id")}
