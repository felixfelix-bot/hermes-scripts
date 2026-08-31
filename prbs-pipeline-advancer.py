#!/usr/bin/env python3
"""PRBS pipeline advancer — advances the PRBS task chain on firmware-harmonization board.

Zero-LLM watchdog: unblocks downstream tasks when upstreams done, re-blocks
prematurely promoted tasks, reclaims stale running tasks, announces completion.

Chain:
  Wave 1 (parallel): PRBS-1, PRBS-2, PRBS-5
  Wave 2 (parallel after Wave 1):
    PRBS-3 needs PRBS-2
    PRBS-4 needs PRBS-2
    PRBS-6 needs PRBS-5
    PRBS-7 needs PRBS-5
  Wave 3: PRBS-8 needs PRBS-1, PRBS-3, PRBS-4, PRBS-6, PRBS-7 (ALL)

MEAS chain (separate, also managed here):
  MEAS-1 needs INT-1
  MEAS-2 needs MEAS-1
"""
import subprocess
import sys
import json
import os
import time

BOARD = "firmware-harmonization"
CHAIN = [
    # Wave 2 deps
    (("t_6c2a2610",), "t_9ef9dec4"),   # PRBS-3 needs PRBS-2
    (("t_6c2a2610",), "t_6599e7b0"),   # PRBS-4 needs PRBS-2
    (("t_c4fbad22",), "t_f6dbb78a"),   # PRBS-6 needs PRBS-5
    (("t_c4fbad22",), "t_eab7f5c7"),   # PRBS-7 needs PRBS-5
    # Wave 3 deps (AND — needs ALL Wave 2 + PRBS-1)
    (("t_480a0a37", "t_9ef9dec4", "t_6599e7b0", "t_f6dbb78a", "t_eab7f5c7"), "t_b1746766"),  # PRBS-8 needs ALL
    # MEAS chain
    (("t_0a18531a",), "t_add511ec"),   # MEAS-1 needs INT-1
    (("t_add511ec",), "t_8df5a988"),   # MEAS-2 needs MEAS-1
]
FINAL = "t_b1746766"  # PRBS-8 — cross-rig integration (operator gate)
STALE_HOURS = 2.0


def kb(*args):
    env = dict(os.environ, HERMES_KANBAN_BOARD=BOARD)
    r = subprocess.run(
        ["hermes", "kanban"] + list(args),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return r.returncode, r.stdout.strip() + r.stderr.strip()


def tasks():
    rc, out = kb("ls", "--json")
    if rc != 0:
        return None, f"list failed: {out[:200]}"
    try:
        return json.loads(out), None
    except Exception as e:
        return None, f"json parse failed: {e}"


def main():
    data, err = tasks()
    if data is None:
        print(f"WARN could not read board: {err}")
        return
    by_id = {t.get("id"): t for t in data if t.get("id")}
    if FINAL in by_id and by_id[FINAL].get("status") == "done":
        print(f"PRBS PIPELINE COMPLETE: final task {FINAL} done. Notify operator.")
        return

    actions = []
    for ups, down in CHAIN:
        up_states = [by_id.get(u, {}).get("status") for u in ups]
        down_s = by_id.get(down, {}).get("status")
        all_done = all(s == "done" for s in up_states)
        if all_done and down_s == "blocked":
            kb("unblock", down)
            actions.append(f"unblocked {down} (upstream(s) done: {', '.join(ups)})")
        elif not all_done and down_s == "ready":
            kb("block", down)
            actions.append(f"re-blocked {down} (upstream(s) not all done: {dict(zip(ups, up_states))})")

    # stale running detection
    for t in data:
        if t.get("status") == "running":
            started = t.get("started_at")
            try:
                started = float(started) if started else None
            except (TypeError, ValueError):
                started = None
            if started and (time.time() - started) > STALE_HOURS * 3600:
                kb("reclaim", t["id"])
                actions.append(f"RECLAIMED stale running {t['id']} (>{STALE_HOURS}h)")

    if actions:
        print(f"{BOARD} pipeline advanced:\n- " + "\n- ".join(actions))


if __name__ == "__main__":
    main()