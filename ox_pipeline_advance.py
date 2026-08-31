#!/usr/bin/env python3
"""Kanban pipeline advancer — OX chain (oxalpha promo) on cost-gate-reform board.
Chain: OX-1 -> OX-2 -> OX-3 -> OX-4, with operator gates OX-KEY-GATE -> OX-3 and
OX-D7-GATE -> OX-4. Gate tasks are parentless (vacuous promotion risk — the
dispatcher promotes parentless blocked tasks). This advancer RE-BLOCKS gates
unless their human condition marker file exists; the MANAGER (not this script)
completes gates after Felix's action. COMPLETE = done|review (Gate-6 manager
approval semantics, same as CG advancer). FINAL = OX-4.
Gate markers (created by manager, not here):
  /tmp/ox_key_gate_ready   — OPENROUTER_OXALPHA_KEY landed in ~/.hermes/.env
  /tmp/ox_d7_gate_ready    — Felix D7 verdict received in chat
"""
import subprocess
import json
import os
import time

BOARD = "cost-gate-reform"
T = {
    "OX-1": "t_ce6edf86", "OX-2": "t_2ed46556", "OX-3": "t_55d38878",
    "OX-4": "t_aa11cc50", "KEYGATE": "t_025f6b5a", "D7GATE": "t_165dea27",
}
CHAIN = [
    ((T["OX-1"],), T["OX-2"]),
    ((T["OX-2"], T["KEYGATE"]), T["OX-3"]),
    ((T["OX-3"], T["D7GATE"]), T["OX-4"]),
]
# gate task -> (marker file, human condition text)
GATES = {
    T["KEYGATE"]: ("/tmp/ox_key_gate_ready", "OPENROUTER_OXALPHA_KEY in ~/.hermes/.env"),
    T["D7GATE"]: ("/tmp/ox_d7_gate_ready", "Felix D7 verdict in chat"),
}
FINAL = T["OX-4"]
STALE_HOURS = 3.0
COMPLETE = ("done", "review")
PING_TASK = T["OX-3"]
PING_MARKER = "/tmp/cost_gate_reform_ox3_ping"
PING_TEXT = ("PING: OX-3 shadow campaign finished — read docs/OX3-shadow-report, "
             "get Felix D7 verdict (routing vs teardown), then complete D7 gate.")


def kb(*args):
    env = dict(os.environ, HERMES_KANBAN_BOARD=BOARD)
    r = subprocess.run(["hermes", "kanban"] + list(args),
                       capture_output=True, text=True, env=env, timeout=60)
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
        print(f"OX PIPELINE COMPLETE: {FINAL} done — notify operator, remove this cron.")
        return

    if PING_TASK:
        if by_id.get(PING_TASK, {}).get("status") in COMPLETE and not os.path.exists(PING_MARKER):
            open(PING_MARKER, "w").write(str(time.time()))
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
    # Operator-gate protection: gates stay blocked until their marker exists.
    # Manager completes the gate task itself once the human condition is met —
    # the marker is written by the manager at that moment (not before).
    for g, (marker, cond) in GATES.items():
        st = by_id.get(g, {}).get("status")
        if st in ("ready", "running") and not os.path.exists(marker):
            if st == "running":
                fresh, _ = tasks()
                # never reclaim a gate the manager legitimately completed
                fby = {t.get("id"): t for t in fresh or []}
                if fby.get(g, {}).get("status") in COMPLETE:
                    continue
                kb("reclaim", g, "--reason", "operator gate: condition not met")
                actions.append(f"RECLAIMED gate {g} (spawned without {cond})")
            kb("block", g, "operator gate — human condition pending")
            actions.append(f"re-blocked gate {g} (pending: {cond})")

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
                kb("reclaim", down, "--reason", "watchdog: upstream(s) not done")
                actions.append(f"RECLAIMED premature {down}")
                down_s = "ready"
            kb("block", down, "watchdog: upstream(s) not done")
            actions.append(f"blocked {down} (upstreams: {dict(zip(ups, up_states))})")

    for t in data:
        if t.get("status") == "running" and t.get("id") not in GATES:
            try:
                started = float(t.get("started_at") or 0)
            except (TypeError, ValueError):
                started = 0.0
            if started and (time.time() - started) > STALE_HOURS * 3600:
                kb("reclaim", t["id"])
                actions.append(f"RECLAIMED stale running {t['id']} (>{STALE_HOURS}h)")

    if actions:
        print(f"{BOARD} OX chain advanced:\n- " + "\n- ".join(actions))


if __name__ == "__main__":
    main()
