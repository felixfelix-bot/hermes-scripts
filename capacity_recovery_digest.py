#!/usr/bin/env python3
"""capacity_recovery_digest.py — one digest when a capacity outage ends (K3).

Option-C notification policy (operator 2026-09-17):
  * proxy sentinel ``~/.hermes/bot/.capacity_outage`` (start ts) marks an outage;
  * the gateway emits ONE start notice (throttled) while it is set;
  * when the sentinel CLEARS (capacity returns) this script emits a single
    recovery digest listing what was held (``.capacity_held.jsonl``) and clears
    that ledger.

Silent otherwise (no sentinel transition). Exit always 0.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
SENTINEL = BOT / ".capacity_outage"
STATE = BOT / ".capacity_digest_state.json"
HELD = BOT / ".capacity_held.jsonl"


def _read(p: Path, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d


def main() -> int:
    was = bool(_read(STATE, {}).get("outage_active", False))
    now_active = SENTINEL.exists()
    started = 0
    try:
        started = int(SENTINEL.read_text().strip() or "0")
    except Exception:
        started = 0
    STATE.write_text(json.dumps({"outage_active": now_active, "outage_started": started}))

    # Recovery transition: was in an outage, now not.
    if was and not now_active:
        labels = []
        try:
            for line in HELD.read_text().splitlines():
                try:
                    labels.append(str(json.loads(line).get("label") or "?"))
                except Exception:
                    pass
        except Exception:
            pass
        n = len(labels)
        uniq = sorted(set(labels))
        began = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        msg = (f"✅ Model capacity restored {time.strftime('%Y-%m-%d %H:%M')} "
               f"(outage started {began}). Held during outage: {n}")
        if uniq:
            msg += " — " + ", ".join(uniq[:12]) + ("..." if len(uniq) > 12 else "")
        try:
            HELD.unlink()
        except Exception:
            pass
        print(msg)  # non-empty stdout -> delivered to the operator
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
