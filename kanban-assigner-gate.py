#!/usr/bin/env python3
"""kanban-assigner-gate.py — D5 wake gate for the kanban-auto-assigner cron.

Runs as the job's pre-script and decides DETERMINISTICALLY whether an LLM
session is warranted. The scheduler contract: if the last stdout line is
{"wakeAgent": false} (exit 0), the LLM session is skipped entirely — zero
tokens, [SILENT] delivery preserved.

Before D5: kanban_auto_assigner.py ran in report mode, printed NO_ACTION ~89%
of runs (1639/1837 audited), and the scheduler still spawned a ~200KB-context
LLM session that echoed [SILENT]. Now the script does all the deciding:

  SLEEP (no LLM) when ANY of:
    * z.ai quota exhausted for both keys (zai_state.json)
    * no ready+unassigned tasks on any board
    * no idle worker profile available
    * Kalman pool at capacity (remaining_slots <= 0)

  WAKE (LLM runs `kanban_auto_assigner.py --auto`) when an assignment is
    genuinely possible — scan summary is printed for the agent's context.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def sleep_gate(reason: str) -> None:
    print(f"# GATE-CLOSED: {reason}")
    print('{"wakeAgent": false}')
    sys.exit(0)


def wake_gate(summary_lines: list) -> None:
    for line in summary_lines:
        print(line)
    print('{"wakeAgent": true}')
    sys.exit(0)


def quota_ok() -> bool:
    """Same logic as the old in-prompt gate: proceed if EITHER z.ai key has
    headroom. No state file = assume OK (fail-open)."""
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

    import kanban_auto_assigner as kaa

    all_tasks = kaa.scan_all_boards_fast()
    ready = [t for t in all_tasks
             if t["status"] == "ready" and not t["assignee"]]
    if not ready:
        sleep_gate("no ready+unassigned tasks found")

    busy = kaa.get_busy_profiles()
    profiles = kaa.get_profile_status()
    idle = sorted(
        name for name, info in profiles.items()
        if name.startswith("worker-") and name not in busy
        and info.get("on_disk", False))
    if not idle:
        sleep_gate(
            f"{len(ready)} ready+unassigned tasks but ALL worker profiles busy")

    # Kalman pool cap (mirrors kanban_auto_assigner.py main() logic).
    pool_cap = len(profiles)
    pool_note = "no kalman state"
    pool_state_path = os.path.expanduser("~/.hermes/state/pool_kalman.json")
    try:
        with open(pool_state_path) as f:
            ps = json.load(f)
        smoothed = int(round(ps["x"][0]))
        pool_cap = max(1, min(smoothed, len(profiles)))
        pool_note = f"kalman smoothed={smoothed}, cap={pool_cap}"
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    running = len([n for n in profiles
                   if n in busy and n.startswith("worker-")])
    remaining = max(0, pool_cap - running)
    if remaining <= 0:
        sleep_gate(
            f"pool at capacity ({running}/{pool_cap} running, {pool_note}) — "
            f"{len(ready)} tasks would be assigned to nobody")

    lines = [f"=== Auto-Assigner WAKE: {len(ready)} ready+unassigned, "
             f"{len(idle)} idle workers ({', '.join(idle)}), "
             f"pool {running}/{pool_cap} ({pool_note}) ==="]
    for t in ready[:15]:
        lines.append(f"  {t['board']}/{t['id']}: {t['title'][:80]}")
    if len(ready) > 15:
        lines.append(f"  ... and {len(ready) - 15} more")
    lines.append("")
    lines.append("Action: run `kanban_auto_assigner.py --auto` to assign now.")
    wake_gate(lines)


if __name__ == "__main__":
    main()
