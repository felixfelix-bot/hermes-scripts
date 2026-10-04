#!/usr/bin/env python3
"""operator_alert.py — post a short, rate-limited alert to the operator channel.

Shared by fleet watchdogs so a *soft* failure (pool priced out, sustained
non-200, empty pool) can ALERT the operator instead of restarting Hermes.
Restarting clears in-memory latches but destroys diagnostic state and hides the
real cause; the router self-heals, so a watchdog should surface the condition.

Never raises. Missing relay/key/config is a non-fatal no-op (exit 0).

Usage:
  operator_alert.py --topic router-watchdog --text "..." [--cooldown 3600]
                    [--tag t=router] [--json]
Exit: 0 posted or suppressed-by-cooldown; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BOT = Path.home() / ".hermes" / "bot"
ALERTS_CFG = BOT / "alerts_channel.json"
STATE = BOT / ".operator_alert_state.json"
NSEC = Path.home() / ".hermes" / "keys" / "hermes-ops" / "cobrador.nsec"


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        BOT.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
    except Exception:
        pass


def trimmed(text: str, limit: int = 900) -> str:
    """Collapse whitespace and cap length (Buzz/Signal have size limits)."""
    return " ".join(str(text or "").split())[:limit]


def post_alert(text: str, *, topic: str, cooldown_s: float = 3600.0,
               tag: str = "t=fleet-alert", now: float | None = None) -> str:
    """Post *text* to the operator channel unless topic is in cooldown.

    Returns "posted", "suppressed" or "unconfigured".
    """
    now = now if now is not None else time.time()
    state = _load_state()
    last = float((state.get(topic) or {}).get("last", 0) or 0)
    if cooldown_s > 0 and now - last < cooldown_s:
        return "suppressed"

    cfg = {}
    try:
        cfg = json.loads(ALERTS_CFG.read_text())
    except Exception:
        pass
    group = cfg.get("orange_group")
    nsec_path = Path(os.path.expanduser(cfg.get("nsec") or str(NSEC)))
    if not nsec_path.exists():
        nsec_path = NSEC
    if not group or not nsec_path.exists():
        return "unconfigured"
    try:
        r = subprocess.run(
            [_nak(), "event", "-k", "9", "-t", f"h={group}", "-t", tag,
             "-t", "client=hermes-fleet", "-c", trimmed(text),
             "--auth", "--sec", nsec_path.read_text().strip(),
             cfg.get("relay", "wss://relay.orangesync.tech")],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        ok = "success" in (r.stdout + r.stderr)
    except Exception:
        ok = False
    state[topic] = {"last": now}
    _save_state(state)
    return "posted" if ok else "failed"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--cooldown", type=float, default=3600.0)
    ap.add_argument("--tag", default="t=fleet-alert")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = post_alert(args.text, topic=args.topic, cooldown_s=args.cooldown,
                     tag=args.tag)
    if args.json:
        print(json.dumps({"result": res, "topic": args.topic}))
    else:
        print(f"operator_alert: {res} (topic={args.topic})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
