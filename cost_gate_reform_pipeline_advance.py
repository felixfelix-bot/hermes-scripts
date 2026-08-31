#!/usr/bin/env python3
"""Kanban pipeline advancer — cost-gate-reform board (CG-1..CG-9).
Watchdog cron template v2 adapted 2026-08-21. Chain per PLAN-cost-gate-reform-v2:
CG-1 -> {CG-2, CG-3, CG-4}; CG-2 -> CG-6; {CG-1,CG-2,CG-3,CG-4} -> CG-7;
CG-7 -> {CG-8, CG-9}. CG-5 independent root (not in chain).
Note: tasks complete to 'review' (Gate 6, manager approval) — treat BOTH
'review' and 'done' as complete for unblock purposes. Manager reviews and
completes. FINAL = CG-9.
"""
import subprocess
import json
import os
import time

# ==== CONFIG ====
BOARD = "cost-gate-reform"
T = {
    "CG-1": "t_86964b82", "CG-2": "t_7d1634d9", "CG-3": "t_64e4273b",
    "CG-4": "t_f55b954a", "CG-5": "t_852bbe0d", "CG-6": "t_d82d4031",
    "CG-7": "t_a75747b8", "CG-8": "t_3b8587bc", "CG-9": "t_61cd92db",
}
CHAIN = [
    ((T["CG-1"],), T["CG-2"]),
    ((T["CG-1"],), T["CG-3"]),
    ((T["CG-1"],), T["CG-4"]),
    ((T["CG-2"],), T["CG-6"]),
    ((T["CG-1"], T["CG-2"], T["CG-3"], T["CG-4"]), T["CG-7"]),
    ((T["CG-7"],), T["CG-8"]),
    ((T["CG-7"],), T["CG-9"]),
]
FINAL = T["CG-9"]
STALE_HOURS = 3.0
COMPLETE = ("done", "review")   # review = done pending manager Gate-6 approval
PING_TASK = T["CG-7"]
PING_MARKER = "/tmp/cost_gate_reform_cg7_ping"
PING_TEXT = ("PING: CG-7 (gate CLI + shims) landed — run 3 spot-check crons, "
             "then CG-8 consolidation (manager lane).")
# ================


def kb(*args):
    env = dict(os.environ, HERMES_KANBAN_BOARD=BOARD)
    r = subprocess.run(
        ["hermes", "kanban"] + list(args),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


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
    if FINAL in by_id and by_id[FINAL].get("status") in COMPLETE:
        print(f"PIPELINE COMPLETE: {FINAL} done — board finished, notify operator, remove this cron.")
        return

    if PING_TASK:
        marker = PING_MARKER
        if by_id.get(PING_TASK, {}).get("status") in COMPLETE and not os.path.exists(marker):
            open(marker, "w").write(str(time.time()))
            print(PING_TEXT)

    for ups, down in CHAIN:
        for u in ups:
            kb("link", u, down)

    data, err = tasks()
    if data is None:
        print(f"WARN could not re-read board after link self-heal: {err}")
        return
    by_id = {t.get("id"): t for t in data if t.get("id")}

    actions = []
    for ups, down in CHAIN:
        up_states = [by_id.get(u, {}).get("status") for u in ups]
        down_s = by_id.get(down, {}).get("status")
        all_done = all(s in COMPLETE for s in up_states)
        if all_done and down_s == "blocked":
            kb("unblock", down)
            actions.append(f"unblocked {down} (upstream done: {', '.join(ups)})")
        elif not all_done and down_s in ("ready", "running"):
            if down_s == "running":
                fresh, _ = tasks()
                fby = {t.get("id"): t for t in fresh or []}
                if all(fby.get(u, {}).get("status") in COMPLETE for u in ups):
                    continue
                kb("reclaim", down, "--reason",
                   "watchdog: upstream(s) not done — premature spawn")
                actions.append(
                    f"RECLAIMED premature {down} (worker killed; upstreams: "
                    f"{dict(zip(ups, [fby.get(u, {}).get('status') for u in ups]))})")
                down_s = "ready"
            kb("block", down, "watchdog: upstream(s) not done")
            actions.append(f"blocked {down} (gate: upstreams not all done: {dict(zip(ups, up_states))})")

    for t in data:
        if t.get("status") == "running":
            try:
                started = float(t.get("started_at") or 0)
            except (TypeError, ValueError):
                started = 0.0
            if started and (time.time() - started) > STALE_HOURS * 3600:
                kb("reclaim", t["id"])
                actions.append(f"RECLAIMED stale running {t['id']} (>{STALE_HOURS}h)")

    if actions:
        print(f"{BOARD} pipeline advanced:\n- " + "\n- ".join(actions))


if __name__ == "__main__":
    main()
