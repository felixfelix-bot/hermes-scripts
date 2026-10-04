#!/usr/bin/env python3
"""fleet_deadman.py — watch-the-watchdog for the two-node fleet.

The arbiter on each node acts on *fresh* peer health. If a node's health feed
goes stale (its fleet-health timer died, the box is hung, or the network split),
that node can't be sensed by the normal path. This dead-man's switch alerts on
stale peers and, when SSH still works, nudges the peer's health timer back up.

It also watches *fresh* peer heartbeats for a corrupt `state.db`
(`components.state_db == down`): it pages, and when the state_db_guard policy
sets `state_db_peer_autorepair` (default off) it SSH-invokes the peer's
loss-aware auto-repair — the backstop for a peer whose own guard is not acting.

Runs every 5 min on both nodes; dedups alerts per peer per cooldown.

Usage:
  fleet_deadman.py [--push] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
PEERS = BOT / "peers"
FLEET_CFG = BOT / "fleet.json"
STATE = BOT / "fleet_deadman_state.json"
OPS_CFG = BOT / "hermes_ops.json"
LEDGER = BOT / "fleet_interventions.jsonl"
STALE_S = 600          # 10 min without any peer signal
REALERT_S = 3600       # re-alert at most hourly per peer


def _read_json(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _write_json(p: Path, payload) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(p)


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _publish(content: str) -> bool:
    ops = _read_json(OPS_CFG, {})
    group = ops.get("orange_group")
    nsec = Path(os.path.expanduser(ops.get("node_nsec", "~/.hermes/keys/hermes-ops/cobrador.nsec")))
    if not group or not nsec.exists():
        return False
    try:
        key = nsec.read_text().strip()
    except OSError:
        return False
    cmd = [_nak(), "event", "-k", "9", "-c", content, "-t", f"h={group}",
           "-t", "client=hermes-fleet", "-t", "t=fleet-alert", "--sec", key,
           "--auth", ops.get("orange_relay", "wss://relay.orangesync.tech")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        return "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def _ssh_nudge(peer: dict) -> bool:
    """Best-effort: restart the peer's fleet-health timer if SSH works."""
    hosts = peer.get("hosts") or ([peer.get("host")] if peer.get("host") else [])
    user = peer.get("user", "c03rad0r")
    key = os.path.expanduser(peer.get("key", "~/.ssh/id_dq05"))
    for host in hosts:
        if not host:
            continue
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=6", "-i", key, f"{user}@{host}",
                 "systemctl --user start fleet-health.service"],
                capture_output=True, text=True, timeout=25)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _peer_state_db_down(heartbeat: dict) -> str | None:
    """Detail string if a peer heartbeat reports a corrupt state.db, else None."""
    comp = ((heartbeat or {}).get("components") or {}).get("state_db") or {}
    if comp.get("status") == "down":
        return str(comp.get("detail") or "state_db down")
    return None


def _ssh_repair_state_db(peer: dict) -> bool:
    """Best-effort SSH run of the peer's loss-aware state.db auto-repair.

    Gated by the state_db_guard policy (``state_db_peer_autorepair``, default
    off): the peer's own component guard + state-db-guard timer are the primary
    path; this is the dead-man backstop for a peer whose own watchdog is not
    acting.
    """
    hosts = peer.get("hosts") or ([peer.get("host")] if peer.get("host") else [])
    user = peer.get("user", "c03rad0r")
    key = os.path.expanduser(peer.get("key", "~/.ssh/id_dq05"))
    for host in hosts:
        if not host:
            continue
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=6", "-i", key, f"{user}@{host}",
                 "python3 ~/.hermes/scripts/state_db_autorepair.py --apply --json"],
                capture_output=True, text=True, timeout=1500)
            if r.returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = _read_json(FLEET_CFG, {})
    me = cfg.get("node") or socket.gethostname()
    peers_cfg = {p.get("name"): p for p in cfg.get("peers", []) or []}
    st = _read_json(STATE, {})
    now = time.time()

    findings = []
    seen = set()
    for f in sorted(PEERS.glob("*.json")):
        h = _read_json(f, None)
        if not h:
            continue
        node = h.get("node") or "?"
        if node in seen or node == me:
            continue
        seen.add(node)
        age = now - float(h.get("ts", 0) or 0)
        if age <= STALE_S:
            continue
        last = float(st.get(node, 0) or 0)
        if now - last < REALERT_S:
            continue
        peer = peers_cfg.get(node, {})
        nudged = _ssh_nudge(peer)
        msg = (f"DEAD-MAN: peer {node} health stale {int(age)}s "
               f"(ssh_nudge={'ok' if nudged else 'unreachable'})")
        findings.append({"peer": node, "age_s": int(age), "nudged": nudged})
        entry = {"ts": now, "node": me, "action": "deadman-notify",
                 "target": node, "reason": msg, "result": f"nudge={nudged}"}
        try:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass
        st[node] = now

    # Cross-node state.db watchdog (2026-09-28): a FRESH peer heartbeat that
    # reports a corrupt state.db is paged here. The actual repair is gated by
    # state_db_guard.json (state_db_peer_autorepair, default off) because the
    # peer's own component guard + state-db-guard timer are the primary path.
    policy = _read_json(BOT / "state_db_guard.json", {}) or {}
    peer_repair = bool(policy.get("state_db_peer_autorepair", False))
    for f in sorted(PEERS.glob("*.health.json")):
        h = _read_json(f, None)
        if not h:
            continue
        node = h.get("node") or "?"
        if node == me:
            continue
        detail = _peer_state_db_down(h)
        if not detail:
            continue
        key = f"state_db:{node}"
        if now - float(st.get(key, 0) or 0) < REALERT_S:
            continue
        repaired = _ssh_repair_state_db(peers_cfg.get(node, {})) if peer_repair else False
        result = "repaired" if repaired else ("gated-off" if not peer_repair else "unreachable")
        msg = f"peer {node} state.db DOWN: {detail} (peer_repair={result})"
        findings.append({"peer": node, "kind": "state_db", "detail": detail,
                         "peer_repair": result})
        try:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps({"ts": now, "node": me,
                                     "action": "deadman-state-db", "target": node,
                                     "reason": msg, "result": result},
                                    separators=(",", ":")) + "\n")
        except OSError:
            pass
        st[key] = now

    _write_json(STATE, st)
    if args.push and findings:
        _publish("fleet-alert (dead-man) " + json.dumps({
            "type": "fleet-alert", "node": me, "ts": int(now),
            "findings": findings}, separators=(",", ":")))
    if args.json:
        print(json.dumps({"self": me, "findings": findings}, indent=1))
    elif findings:
        for f in findings:
            print(f"[deadman] {f['peer']} stale {f['age_s']}s nudged={f['nudged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
