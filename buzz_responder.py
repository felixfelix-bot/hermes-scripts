#!/usr/bin/env python3
"""buzz_responder.py — Klaus/Felix answer operator messages on Buzz (D-129).

Watches the shared fleet OrangeSync relay for messages from the operator's
pubkey(s) and replies with a Hermes agent turn. Several fleet nodes run this
service; exactly one answers each message:

  * a message that p-tags one of my pubkeys -> I answer directly;
  * an untagged message -> the resource-elected leader answers;
  * a message tagged to a peer -> held for a grace window, then answered by me
    if that peer is stale/offline (failover);
  * DMs (kind 4 / NIP-59) -> the addressed node answers.

The election is deterministic (all nodes share Kalman-smoothed capacity via the
SSH heartbeat) and backed by a short claim handshake on a private coordination
group (see fleet_claim.py / responder_election.py). A message is never answered
twice.

Config: ~/.hermes/bot/buzz_responder.json (defaults created on first run)
  {"relay": "...", "group": "...", "all_groups": true,
   "operators": ["<hex|npub>"], "operator_pubkey": "<hex|npub>",
   "nsec_path": "~/.hermes/keys/hermes-ops/dq05.nsec",
   "profile": "worker-base", "agent_name": "Klaus", "poll_s": 6,
   "election": {"enabled": true, "coord_group": "<uuid>", "claim_window_s": 2,
                "stale_after_s": 120, "min_headroom": 0.05, "hysteresis": 0.15,
                "tag_grace_s": 30, "claim_ttl_s": 30}}

Usage: buzz_responder.py [--once] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__)
sys.path.insert(0, str(_here.resolve().parent))  # repo dir when symlinked
sys.path.insert(0, str(_here.parent))           # ~/.hermes/scripts otherwise
try:
    import responder_election as elect
except Exception:  # pragma: no cover
    elect = None
try:
    import fleet_claim as claim
except Exception:  # pragma: no cover
    claim = None

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
CONFIG = BOT / "buzz_responder.json"
STATE = BOT / "buzz_responder_state.json"
PEERS = BOT / "peers"
FLEET_CFG = BOT / "fleet.json"

DEFAULTS = {
    "relay": "wss://relay.orangesync.tech",
    "relays": [],
    "group": "1389391e-6721-4a3f-9e44-2bbd2f26ddf0",
    "groups": [],
    "all_groups": True,
    "operator_pubkey": "1a31189f46e89d327e6a4fa26376ba5fa81caaec453fab13b1c2f8245e42ba9d",
    "operators": [],
    "nsec_path": "~/.hermes/keys/hermes-ops/dq05.nsec",
    "extra_keys": [],
    "profile": "worker-base",
    "poll_s": 6,
    "reply_timeout_s": 240,
    "max_reply_chars": 4000,
    "dms": True,
    "agent_name": "Klaus",
    "enabled": True,
    "election": {
        "enabled": True,
        "coord_group": "",
        "claim_window_s": 2.0,
        "stale_after_s": 120,
        "min_headroom": 0.05,
        "hysteresis": 0.15,
        "tag_grace_s": 30,
        "claim_ttl_s": 30,
    },
}


def _operators(c: dict) -> set[str]:
    ops = {c.get("operator_pubkey", "")} | set(c.get("operators") or [])
    return {o for o in ops if o}


def log(*p) -> None:
    print(f"[buzz-responder] {datetime.now(timezone.utc).isoformat()}",
          *p, flush=True)


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _read(p: Path, d=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d if d is not None else {}


def cfg() -> dict:
    d = json.loads(json.dumps(DEFAULTS))
    try:
        d.update(json.loads(CONFIG.read_text()))
    except Exception:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG.write_text(json.dumps(d, indent=1))
        except OSError:
            pass
    # keep nested election defaults if a partial block was provided
    el = dict(DEFAULTS["election"])
    el.update((d.get("election") or {}))
    d["election"] = el
    return d


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"since": int(time.time()) - 5, "seen": []}


def _save_state(s: dict) -> None:
    try:
        s["seen"] = s.get("seen", [])[-2000:]
        s["answered"] = s.get("answered", [])[-2000:]
        pend = s.get("pending") or {}
        if len(pend) > 50:
            keep = sorted(pend.items(), key=lambda kv: kv[1].get("first_seen", 0))[-50:]
            s["pending"] = dict(keep)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s, indent=1))
        tmp.replace(STATE)
    except OSError:
        pass


def _key(c: dict) -> str:
    return Path(os.path.expanduser(c["nsec_path"])).read_text().strip()


def _pubkey(nsec: str) -> str:
    try:
        return subprocess.run([_nak(), "key", "public", nsec],
                              capture_output=True, text=True,
                              timeout=15).stdout.strip()
    except Exception:
        return ""


def _my_pubkeys(c: dict) -> set[str]:
    """Node identity only (+ explicit extra_keys); never a shared identity."""
    keys: set[str] = set()
    try:
        keys.add(_pubkey(_key(c)))
    except Exception:
        pass
    for k in c.get("extra_keys") or []:
        if k:
            keys.add(k)
    keys.discard("")
    return keys


def _fleet_pubkeys(c: dict, my_keys: set[str]) -> set[str]:
    """All fleet node identities: peers' heartbeats, plus coordination-group
    membership as a fallback so a tagged peer is never mistaken for untagged."""
    pubkeys = set(my_keys)
    for hb in _peer_heartbeats():
        if hb.get("pubkey"):
            pubkeys.add(hb["pubkey"])
    try:
        rec = _read(BOT / "coord_group.json", {})
        for m in rec.get("members") or []:
            if isinstance(m, str) and len(m) == 64:
                pubkeys.add(m)
    except Exception:
        pass
    return pubkeys


def _peer_heartbeats() -> list[dict]:
    best: dict[str, dict] = {}
    for f in sorted(PEERS.glob("*.json")) if PEERS.exists() else []:
        hb = _read(f, None)
        if not (isinstance(hb, dict) and hb.get("ts") and
                ("load1_per_cpu" in hb or "nproc" in hb)):
            continue
        node = hb.get("node") or f.stem
        if node not in best or hb.get("ts", 0) >= best[node].get("ts", 0):
            best[node] = hb
    return list(best.values())


def _self_heartbeat() -> dict:
    hb = _read(BOT / "fleet_heartbeat.json", {})
    if hb.get("ts"):
        return hb
    # minimal fallback so we can still be ranked before the first heartbeat
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            mem[k.strip()] = int(rest.split()[0])
    except Exception:
        pass
    node = _read(FLEET_CFG, {}).get("node") or os.uname().nodename
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    return {"node": node, "ts": int(time.time()),
            "load1_per_cpu": round(load / (os.cpu_count() or 1), 2),
            "nproc": os.cpu_count() or 1,
            "mem_total_mb": total // 1024, "mem_available_mb": avail // 1024,
            "hermes_workers": 0}


def _node_name(c: dict, hb: dict | None = None) -> str:
    return ((hb or {}).get("node") or _read(FLEET_CFG, {}).get("node")
            or os.uname().nodename)


def _ranked(c: dict, now: float) -> list[dict]:
    if elect is None:
        return []
    stale = float(c["election"].get("stale_after_s", 120))
    nodes = []
    selfhb = _self_heartbeat()
    nodes.append({"node": selfhb.get("node") or _node_name(c), "hb": selfhb,
                  "ts": selfhb.get("ts", 0)})
    for hb in _peer_heartbeats():
        nodes.append({"node": hb.get("node"), "hb": hb, "ts": hb.get("ts", 0)})
    return elect.rank(nodes, now=now, stale_after_s=stale)


def _update_leader(c: dict, s: dict, ranked: list[dict]) -> str | None:
    if elect is None:
        return None
    hyst = float(c["election"].get("hysteresis", 0.15))
    leader = elect.choose_leader(s.get("leader"), ranked, hysteresis=hyst)
    if leader != s.get("leader"):
        s["leader"] = leader
        log(f"leader -> {leader} (ranked=" +
            ", ".join(f"{r['node']}:{r['score']:.2f}{'' if r['alive'] else '!'}"
                      for r in ranked) + ")")
    return leader


def _score_of(ranked: list[dict], node: str) -> float:
    r = next((x for x in ranked if x["node"] == node), None)
    return float(r["score"]) if r else 0.0


def _pubkey_alive(pub: str) -> bool:
    if not pub:
        return False
    for hb in _peer_heartbeats():
        if hb.get("pubkey") == pub:
            return True
    return False


def _tags(ev: dict, name: str) -> list[str]:
    return [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == name]


def fetch_new(c: dict, s: dict) -> list[dict]:
    """Return operator messages in scope (election decides who answers)."""
    since = int(s.get("since", time.time() - 5))
    mine = _my_pubkeys(c)
    ops = sorted(_operators(c))
    args = [_nak(), "req", "--auth", "--sec", _key(c), "-k", "9", "-k", "1"]
    for o in ops:
        args += ["-a", o]
    args += ["--since", str(since), "--limit", "200", c["relay"]]
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
        if ev.get("pubkey") not in ops:
            continue
        # Advance the cursor past EVERY operator event fetched (see
        # decisions_responder, 2026-09-30): the group/tag filtering below must
        # not freeze `since`, or the same window is re-downloaded every poll.
        try:
            _cat = int(ev.get("created_at", 0))
        except (TypeError, ValueError):
            _cat = 0
        if _cat + 1 > int(s.get("since", 0)):
            s["since"] = _cat + 1
        eid = ev.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        tagged = set(_tags(ev, "p"))
        groups = set(_tags(ev, "h"))
        allowed = set(c.get("groups") or [])
        if ((tagged & mine) or c.get("all_groups")
                or (c.get("group") in groups) or (allowed & groups)):
            out.append(ev)
    s["seen"] = list(seen)
    return out


def _answered(s: dict) -> set[str]:
    return set(s.get("answered", []))


def _claim_and_reply(c: dict, ev: dict, s: dict, headroom: float,
                     elected: bool) -> bool:
    """Claim the message; reply only if we win (or it is directly addressed)."""
    eid = ev.get("id")
    el = c["election"]
    election_on = bool(el.get("enabled", True) and el.get("coord_group"))
    if claim is None or not election_on:
        if elected:
            return False
        return _reply_group(c, ev, s)
    if eid in _answered(s):
        return False
    node = _node_name(c, _self_heartbeat())
    claim.publish_claim(c, eid, headroom, node=node)
    if elected:
        time.sleep(float(el.get("claim_window_s", 2.0)))
    since = time.time() - float(el.get("claim_ttl_s", 30)) * 4
    if eid in claim.fetch_done(c, since):
        s.setdefault("answered", []).append(eid)
        log(f"{eid[:12]} already answered by a peer — skip")
        return False
    if elected:
        winner = elect.pick_winner(claim.fetch_claims(c, eid, since),
                                   ttl=float(el.get("claim_ttl_s", 30))) if elect else node
        if winner != node:
            log(f"{eid[:12]} lost election to {winner} — skip")
            return False
    return _reply_group(c, ev, s)


def _reply_group(c: dict, ev: dict, s: dict) -> bool:
    content = (ev.get("content") or "").strip()
    if not content:
        return False
    log(f"incoming from operator: {content[:80]!r}")
    text = reply_text(c, content)
    ok = publish_reply(c, ev, text)
    if ok:
        s.setdefault("answered", []).append(ev.get("id"))
        if claim is not None and c["election"].get("coord_group"):
            claim.publish_done(c, ev.get("id"),
                               node=_node_name(c, _self_heartbeat()))
        log(f"replied ({len(text)} chars)")
    return ok


def reply_text(c: dict, operator_msg: str) -> str:
    """Generate a reply using a Hermes agent turn; fall back to a short ack."""
    name = c.get("agent_name", "Klaus")
    prompt = (
        f"You are {name}, the Hermes fleet agent. The operator sent this message "
        "on the Buzz fleet channel. Reply concisely and directly on the channel "
        "(plain text, no markdown headers). Do not run long jobs; if action is "
        "needed, say what you will do and keep it short.\n\n"
        f"Operator message:\n{operator_msg}"
    )
    cmd = [_hermes_bin(), "-p", c.get("profile", "worker-base"), "chat",
           "-Q", "-q", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=int(c.get("reply_timeout_s", 240)))
        noise = ("Warning:", "⚠", "Error:", "Query:", "Initializing agent",
                 "session_id:", "─")
        lines = []
        for ln in (r.stdout or "").splitlines():
            s = ln.strip()
            if not s or s.startswith(noise):
                continue
            lines.append(ln)
        text = "\n".join(lines).strip()
        if text:
            return text[: int(c.get("max_reply_chars", 4000))]
        log("agent reply empty", (r.returncode, (r.stderr or "").strip()[:160]))
    except Exception as exc:  # noqa: BLE001
        log("agent reply failed:", exc)
    return f"{c.get('agent_name', 'Klaus')} received your message and is on it."


def _hermes_bin() -> str:
    for c in (HERMES / "hermes-agent/venv/bin/hermes",
              Path.home() / ".local/bin/hermes"):
        if c.exists():
            return str(c)
    return "hermes"


def _run(args: list[str], timeout: int = 30, input_text: str | None = None):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, input=input_text)
    except Exception as exc:  # noqa: BLE001
        log("run error:", exc)
        return None


def _relays(c: dict) -> list[str]:
    rs = [c.get("relay")] + list(c.get("relays") or [])
    return [r for r in dict.fromkeys(rs) if r]


def _decrypt(c: dict, sender: str, content: str) -> str | None:
    if not content:
        return None
    for flag in ([], ["--nip04"]):
        r = _run([_nak(), "decrypt", "--sec", _key(c), "-p", sender] + flag
                 + [content], 20)
        if r and r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def _encrypt(c: dict, recipient: str, text: str, nip04: bool = False) -> str | None:
    args = [_nak(), "encrypt", "--sec", _key(c), "-p", recipient]
    if nip04:
        args.append("--nip04")
    args.append(text)
    r = _run(args, 20)
    return r.stdout.strip() if r and r.returncode == 0 and r.stdout.strip() else None


def fetch_dms(c: dict, s: dict) -> list[dict]:
    """Return operator DMs to any of my pubkeys (kind 4 + NIP-59 gift wrap)."""
    if not c.get("dms", True):
        return []
    mine = sorted(_my_pubkeys(c))
    ops = _operators(c)
    if not mine:
        return []
    since = int(s.get("dm_since", s.get("since", time.time() - 5)))
    seen = set(s.get("seen", []))
    out: list[dict] = []
    for relay in _relays(c):
        args = [_nak(), "req", "--auth", "--sec", _key(c), "-k", "4", "-k", "1059"]
        for m in mine:
            args += ["-t", f"p={m}"]
        args += ["--since", str(since), "--limit", "100", relay]
        r = _run(args, 30)
        if not r:
            continue
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = ev.get("id")
            if not eid or eid in seen:
                continue
            sender, text = ev.get("pubkey", ""), None
            if ev.get("kind") == 1059:
                u = _run([_nak(), "gift", "unwrap", "--sec", _key(c)], 20,
                         input_text=line)
                try:
                    rumor = json.loads((u.stdout if u else "") or "{}")
                    sender = rumor.get("pubkey", sender)
                    text = rumor.get("content")
                except json.JSONDecodeError:
                    continue
            else:
                text = _decrypt(c, sender, ev.get("content") or "")
            if sender not in ops:
                log(f"dm from non-operator {sender[:12]} — ignored")
                seen.add(eid)
                continue
            s["dm_since"] = max(int(s.get("dm_since", 0)), int(ev.get("created_at", 0)) + 1)
            seen.add(eid)
            ev["_dm_text"] = (text or "").strip()
            out.append(ev)
    s["seen"] = list(seen)
    return out


def publish_dm(c: dict, to_event: dict, text: str) -> bool:
    sender = to_event.get("pubkey", _operators(c) and next(iter(_operators(c))))
    ct = _encrypt(c, sender, text)
    if not ct:
        ct = _encrypt(c, sender, text, nip04=True)
    if not ct:
        log("dm encrypt failed")
        return False
    args = [_nak(), "event", "-k", "4", "-c", ct, "-p", sender,
            "-e", to_event.get("id")]
    args += ["--sec", _key(c), "--auth", c["relay"]]
    r = _run(args, 45)
    ok = bool(r and "success" in (r.stdout + r.stderr))
    if not ok and r:
        log("dm reply failed:", (r.stdout + r.stderr)[-160:])
    return ok


def publish_reply(c: dict, to_event: dict, text: str) -> bool:
    # Reply into the SAME group the operator used, when there is one.
    h = _tags(to_event, "h")
    group = h[0] if h else c.get("group")
    args = [_nak(), "event", "-k", "9", "-c", text]
    if group:
        args += ["-t", f"h={group}"]
    args += ["-t", f"e={to_event.get('id')}",
             "-t", f"p={c['operator_pubkey']}",
             "-t", "client=hermes-fleet",
             "-t", "t=felix-reply",
             "--sec", _key(c), "--auth", c["relay"]]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=45)
        ok = "success" in (r.stdout + r.stderr)
        if not ok:
            log("reply publish failed:", (r.stdout + r.stderr)[-160:])
        return ok
    except Exception as exc:  # noqa: BLE001
        log("reply publish error:", exc)
        return False


def _schedule_pending(c: dict, s: dict, ev: dict, now: float, other: str) -> None:
    pend = s.setdefault("pending", {})
    pend.setdefault(ev.get("id"), {"ev": ev, "first_seen": now, "other": other})


def _process_pending(c: dict, s: dict, ranked: list[dict], my_node: str,
                     dry_run: bool) -> int:
    """Answer messages tagged to a peer whose heartbeat is stale."""
    if elect is None:
        return 0
    el = c["election"]
    grace = float(el.get("tag_grace_s", 30))
    now = time.time()
    n = 0
    for eid, rec in list((s.get("pending") or {}).items()):
        if eid in _answered(s):
            s["pending"].pop(eid, None)
            continue
        if _pubkey_alive(rec.get("other", "")):
            continue
        if not elect.failover_due(rec, now, grace, tagged_alive=False):
            continue
        ev = rec.get("ev") or {}
        log(f"failover: peer for {eid[:12]} is stale after {int(now - rec['first_seen'])}s")
        if dry_run:
            n += 1
        elif _claim_and_reply(c, ev, s, _score_of(ranked, my_node), elected=False):
            n += 1
        s["pending"].pop(eid, None)
    return n


def run_once(c: dict, s: dict, dry_run: bool = False) -> int:
    n = 0
    now = time.time()
    ranked = _ranked(c, now)
    leader = _update_leader(c, s, ranked)
    myhb = _self_heartbeat()
    my_node = myhb.get("node") or _node_name(c)
    my_keys = _my_pubkeys(c)
    all_keys = _fleet_pubkeys(c, my_keys)
    election_on = bool(c["election"].get("enabled", True)
                       and c["election"].get("coord_group"))
    if not election_on:
        log("election disabled/incomplete — tag-scoped replies only")

    for ev in fetch_new(c, s):
        eid = ev.get("id")
        if eid in _answered(s):
            continue
        action = (elect.decide(ev=ev, my_pubkeys=my_keys,
                               all_node_pubkeys=all_keys, leader=leader,
                               my_node=my_node, election=election_on)
                  if elect else ("direct" if _tags(ev, "p") else "skip"))
        content = (ev.get("content") or "").strip()
        if action == "skip" or not content:
            continue
        if action == "wait-tag":
            other = elect.tags_other_node(ev, my_keys, all_keys) if elect else None
            _schedule_pending(c, s, ev, now, other or "")
            continue
        if dry_run:
            log(f"would reply ({action}): {content[:60]!r}")
            n += 1
            continue
        if _claim_and_reply(c, ev, s, _score_of(ranked, my_node),
                            elected=(action == "elected")):
            n += 1

    n += _process_pending(c, s, ranked, my_node, dry_run)

    for ev in fetch_dms(c, s):
        eid = ev.get("id")
        if eid in _answered(s):
            continue
        content = (ev.get("_dm_text") or "").strip()
        if not content:
            continue
        log(f"incoming DM from operator: {content[:80]!r}")
        if dry_run:
            n += 1
            continue
        text = reply_text(c, content)
        if publish_dm(c, ev, text):
            s.setdefault("answered", []).append(eid)
            n += 1
            log(f"replied DM ({len(text)} chars)")

    _save_state(s)
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    c = cfg()
    if not c.get("enabled", True):
        log("disabled via config")
        return 0
    s = _read_state()
    if args.once:
        n = run_once(c, s, dry_run=args.dry_run)
        log(f"once: {n} message(s)")
        return 0
    el = c["election"]
    log(f"watching for operator messages in "
        f"{'all groups' if c.get('all_groups') else c.get('group')} on "
        f"{c['relay']} (poll {c['poll_s']}s, profile={c['profile']}, "
        f"election={'on' if el.get('enabled') and el.get('coord_group') else 'off'})")
    while True:
        try:
            run_once(c, s)
        except Exception as exc:  # noqa: BLE001
            log("loop error:", exc)
        time.sleep(int(c.get("poll_s", 6)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
