#!/usr/bin/env python3
"""buzz_relay_bridge.py — bridge OrangeSync NIP-29 groups and local strfry groups.

Hermes' native `nostr` platform speaks unauthenticated NIP-29 to a local strfry
relay but cannot do NIP-42 AUTH against `wss://relay.orangesync.tech`. `nak` can.
This bridge pairs each OrangeSync group with a local strfry group so each board
becomes its own Hermes session/chat box.

Performance: ONE `nak req --stream` subprocess per relay (not per pair), with a
single subscription covering ALL groups via `#h` — so N board channels cost 2
processes, not 2N.

Config `~/.hermes/bot/hermes_ops.json`:
{
  "orange_relay": "wss://relay.orangesync.tech",
  "node_nsec": "~/.hermes/keys/hermes-ops/cobrador.nsec",
  "local_nsec": "~/.hermes/profiles/manager/keys/nostr_nsec.txt",
  "local_relay": "ws://100.90.101.9:7780",
  "pairs": [
    {"name": "board-admin", "orange_group": "<uuid>", "local_group": "board-admin"}
  ]
}

Loop prevention: own-pubkey skip (orange side), marker tag on every injected
event, global event-id dedup, per-direction cursors.
See docs/PLAN-operator-channel.md §10.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
CONFIG = BOT / "hermes_ops.json"
STATE_PATH = BOT / "buzz_relay_bridge_state.json"
MARKER = "hermes-relay-bridge"
_NAK_CANDIDATES = [
    os.path.expanduser("~/.local/bin/nak"),
    "/usr/local/bin/nak",
    "/usr/bin/nak",
]
NAK = next((c for c in _NAK_CANDIDATES if Path(c).exists()), "nak")

VERBOSE = "--verbose" in sys.argv


def log(*parts) -> None:
    print("[buzz-relay-bridge]", *parts, flush=True)


def vlog(*parts) -> None:
    if VERBOSE:
        log(*parts)


def load_config() -> dict:
    cfg = json.loads(CONFIG.read_text())
    cfg.setdefault("orange_relay", "wss://relay.orangesync.tech")
    cfg.setdefault("local_relay", "ws://127.0.0.1:7780")
    cfg.setdefault("marker", MARKER)
    if not cfg.get("pairs"):
        cfg["pairs"] = [{
            "name": "default",
            "orange_group": cfg.get("orange_group"),
            "local_group": cfg.get("local_group", "hermes-mgr"),
        }]
    return cfg


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: collections.deque = collections.deque(maxlen=8000)
        self.cursors: dict[str, int] = {}
        try:
            data = json.loads(STATE_PATH.read_text())
            self.seen.extend(data.get("seen", [])[-8000:])
            self.cursors = {k: int(v) for k, v in data.get("cursors", {}).items()}
        except Exception:
            pass

    def save(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps({
                "seen": list(self.seen)[-8000:],
                "cursors": self.cursors,
            }))
        except Exception as exc:  # noqa: BLE001
            log("state save failed:", exc)

    def is_new(self, ev: dict, cursor: str) -> bool:
        eid = ev.get("id", "")
        created = int(ev.get("created_at", 0))
        with self.lock:
            if eid and eid in self.seen:
                return False
            if eid:
                self.seen.append(eid)
            if created > self.cursors.get(cursor, 0):
                self.cursors[cursor] = created
            return True

    def since(self, cursor: str) -> int:
        return self.cursors.get(cursor, int(time.time()) - 5)


STATE = State()


def pubkey_of(nsec: str) -> str:
    return subprocess.run(
        [NAK, "key", "public", nsec], capture_output=True, text=True
    ).stdout.strip()


def htag(ev: dict) -> str | None:
    for tag in ev.get("tags", []):
        if len(tag) >= 2 and tag[0] == "h":
            return tag[1]
    return None


def has_marker(ev: dict, marker: str) -> bool:
    for tag in ev.get("tags", []):
        if len(tag) >= 2 and tag[0] == "client" and tag[1] == marker:
            return True
    return False


def nak_stream(relay: str, groups: list[str], nsec: str, auth: bool,
               cursor: str):
    """One persistent subscription covering all groups on a relay."""
    args = [NAK, "req", "--stream", "--sec", nsec]
    if auth:
        args.append("--auth")
    args.append(relay)
    while True:
        since = STATE.since(cursor)
        filt = json.dumps({"kinds": [9], "#h": groups, "since": since})
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            proc.stdin.write(filt + "\n")
            proc.stdin.flush()
            log(f"stream relay={relay} groups={len(groups)} since={since}")
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        except Exception as exc:  # noqa: BLE001
            log(f"stream error relay={relay}:", exc)
        time.sleep(5)


def nak_publish(relay: str, group: str, content: str, nsec: str, auth: bool,
                marker: str) -> bool:
    args = [NAK, "event", "-k", "9", "-c", content,
            "-t", f"h={group}", "-t", f"client={marker}", "--sec", nsec]
    if auth:
        args.append("--auth")
    args.append(relay)
    for attempt in range(3):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=45)
            if r.returncode == 0 and "success" in (r.stdout + r.stderr):
                return True
            log(f"publish retry {attempt} ({group}):", (r.stdout + r.stderr)[-160:])
        except Exception as exc:  # noqa: BLE001
            log("publish error:", exc)
        time.sleep(3 * (attempt + 1))
    return False


def relay_to_local(cfg: dict, pair: dict, orange_nsec: str, orange_pub: str,
                   local_nsec: str) -> None:
    marker = cfg["marker"]
    og = pair["orange_group"]
    lg = pair["local_group"]
    name = pair["name"]
    while True:
        for ev in nak_stream(cfg["orange_relay"], [og], orange_nsec, True,
                             f"{name}:orange"):
            if ev.get("pubkey") == orange_pub or has_marker(ev, marker):
                continue
            if not STATE.is_new(ev, f"{name}:orange"):
                continue
            text = (ev.get("content") or "").strip()
            if not text:
                continue
            vlog(f"[{name}] orange->local:", text[:60])
            nak_publish(cfg["local_relay"], lg, text, local_nsec, False, marker)
            STATE.save()
        time.sleep(2)


def local_to_relay(cfg: dict, pair: dict, local_nsec: str, orange_nsec: str) -> None:
    marker = cfg["marker"]
    og = pair["orange_group"]
    lg = pair["local_group"]
    name = pair["name"]
    while True:
        for ev in nak_stream(cfg["local_relay"], [lg], local_nsec, False,
                             f"{name}:local"):
            # NB: do not skip local_pub — Hermes replies are signed by the manager
            # key and must be forwarded; the marker excludes bridge-injected events.
            if has_marker(ev, marker):
                continue
            if not STATE.is_new(ev, f"{name}:local"):
                continue
            text = (ev.get("content") or "").strip()
            if not text:
                continue
            vlog(f"[{name}] local->orange:", text[:60])
            nak_publish(cfg["orange_relay"], og, text, orange_nsec, True, marker)
            STATE.save()
        time.sleep(2)


def local_to_relay_all(cfg: dict, pairs: list[dict], local_nsec: str,
                       orange_nsec: str) -> None:
    """D-131: ONE local stream covering every local group (strfry honours a
    multi-`#h` filter), routing each event to its pair's Orange group. Collapses
    N per-pair `nak` processes into one (~18 -> 1 on CW), reclaiming memory/CPU.
    OrangeSync still needs one stream per group (single `#h`/REQ)."""
    marker = cfg["marker"]
    by_group = {p["local_group"]: p for p in pairs}
    groups = list(by_group.keys())
    cursor = "local:all"
    while True:
        for ev in nak_stream(cfg["local_relay"], groups, local_nsec, False, cursor):
            if has_marker(ev, marker):
                continue
            if not STATE.is_new(ev, cursor):
                continue
            h = None
            for t in ev.get("tags", []):
                if len(t) >= 2 and t[0] == "h":
                    h = t[1]
                    break
            pair = by_group.get(h)
            if not pair:
                continue
            text = (ev.get("content") or "").strip()
            if not text:
                continue
            vlog(f"[{pair['name']}] local->orange:", text[:60])
            nak_publish(cfg["orange_relay"], pair["orange_group"], text,
                        orange_nsec, True, marker)
            STATE.save()
        time.sleep(2)


def main() -> int:
    cfg = load_config()
    orange_nsec = Path(os.path.expanduser(cfg["node_nsec"])).read_text().strip()
    orange_pub = pubkey_of(orange_nsec)
    local_path = cfg.get("local_nsec") or str(
        HOME / ".hermes/profiles/manager/keys/nostr_nsec.txt")
    local_nsec = Path(os.path.expanduser(local_path)).read_text().strip()
    local_pub = pubkey_of(local_nsec)
    pairs = [p for p in cfg["pairs"] if p.get("orange_group") and p.get("local_group")]
    cap = int(cfg.get("max_pairs", 12))
    if len(pairs) > cap:
        log(f"capping live pairs {len(pairs)} -> {cap} (set max_pairs to raise)")
        pairs = pairs[:cap]
    log(f"orange={orange_pub[:12]}… local={local_pub[:12]}… live_pairs={len(pairs)} "
        f"relay={cfg['orange_relay']} local={cfg['local_relay']}")

    if "--list" in sys.argv:
        for pair in pairs:
            print(f"{pair['name']}: {pair['orange_group']} <-> {pair['local_group']}")
        return 0

    for pair in pairs:
        threading.Thread(target=relay_to_local,
                         args=(cfg, pair, orange_nsec, orange_pub, local_nsec),
                         name=f"o2l-{pair['name']}", daemon=True).start()
    threading.Thread(target=local_to_relay_all,
                     args=(cfg, pairs, local_nsec, orange_nsec),
                     name="l2o-all", daemon=True).start()
    last_save = time.time()
    try:
        while True:
            time.sleep(15)
            if time.time() - last_save > 60:
                STATE.save()
                last_save = time.time()
    except KeyboardInterrupt:
        pass
    STATE.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
