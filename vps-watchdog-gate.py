#!/usr/bin/env python3
"""vps-watchdog-gate.py — D5 wake gate for the "VPS watchdog auto-fix (Tier 1)" cron.

Before D5 this job had NO pre-script: an LLM session spawned every hour just
to read ~/.local/state/vps-dual-watchdog/alert.json and say [SILENT] (1190 of
1275 audited runs were silent, 93%). The alert file is written by
dual-vps-watchdog.py ONLY when a VPS check fails and is DELETED when healthy —
so the decision "is there anything to fix?" is fully deterministic:

  SLEEP (no LLM) when:
    * z.ai quota exhausted for both keys
    * alert.json absent (healthy) or contains an empty failures list

  WAKE (LLM attempts mechanical fixes) when active failures exist — the
    failures are printed into the agent's prompt as its starting context.

Scheduler contract: last stdout line {"wakeAgent": false} + exit 0 skips the
LLM entirely; [SILENT] delivery contract preserved.
"""

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

ALERT_FILE = Path.home() / ".local" / "state" / "vps-dual-watchdog" / "alert.json"
MAX_FAILURES_SHOWN = 20


def sleep_gate(reason: str) -> NoReturn:
    print(f"# GATE-CLOSED: {reason}")
    print('{"wakeAgent": false}')
    sys.exit(0)


def wake_gate(lines: list) -> NoReturn:
    for line in lines:
        print(line)
    print('{"wakeAgent": true}')
    sys.exit(0)


def quota_ok() -> bool:
    state = Path.home() / ".hermes" / "bot" / "zai_state.json"
    try:
        with open(state) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return True
    our_ok = (not d.get("quota_pause") and not d.get("critical")
              and not d.get("throttle")
              and int(d.get("token_pct", 0)) < 80
              and int(d.get("session_pct", 0)) < 85)
    friend_ok = int(d.get("friend_token_pct", 0)) < 80
    return our_ok or friend_ok


def main() -> None:
    if not quota_ok():
        sleep_gate("z.ai quota exhausted (both keys) — skipping to save tokens")

    if not ALERT_FILE.exists():
        sleep_gate(f"no alert file ({ALERT_FILE}) — both VPS healthy")

    try:
        with open(ALERT_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # Unreadable alert file is itself a problem worth an LLM look.
        wake_gate([f"WARN: {ALERT_FILE} exists but unreadable: {e}",
                   "Investigate the dual-vps-watchdog state."])

    failures = data.get("failures", []) if isinstance(data, dict) else []
    if not failures:
        sleep_gate("alert.json present but no active failures — healthy")

    lines = [f"=== VPS WATCHDOG WAKE: {len(failures)} active failure(s) "
             f"(alert_time={data.get('alert_time', '?')}) ==="]
    for f_ in failures[:MAX_FAILURES_SHOWN]:
        lines.append(f"  FAIL: {f_}")
    if len(failures) > MAX_FAILURES_SHOWN:
        lines.append(f"  ... and {len(failures) - MAX_FAILURES_SHOWN} more")
    lines.append("")
    lines.append("Action: attempt mechanical fixes (restart containers, "
                 "SSH reconnect), verify, and stay SILENT if resolved.")
    wake_gate(lines)


if __name__ == "__main__":
    main()
