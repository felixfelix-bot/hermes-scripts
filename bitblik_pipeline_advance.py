#!/usr/bin/env python3
"""Pipeline advancer for bitblik-trust-demo board.

Deterministic dependency chain:
  R1 t_d827c8c4 (relay) -> R2 t_82c29a18 (signed envelope)
    -> R3 t_7d02f952 (demo --relay mode) -> R4 t_5e625d61 (polish)
    -> R5 t_62270951 (Kimi cold review)          [FINAL]

Watchdog v2 (spec: specs/kanban-gating-watchdog-v2.md, incident 2026-08-14):

- Self-heal links: every run re-asserts CHAIN edges via `link` (idempotent
  INSERT OR IGNORE). The dependency chain must live in task_links, not only
  in this config — with zero links recompute_ready() vacuously promotes
  everything and the dispatcher spawns blocked tasks within one 60 s tick.
- Premature-ready: downstream `ready` while upstreams not all done -> block.
- Premature-running (v2): downstream `running` while upstreams not all done
  -> FRESH board re-read (upstream may have completed since the snapshot;
  if so hands off — legitimate run), then `reclaim` (SIGTERM/SIGKILL the
  worker, closes the run as 'reclaimed') THEN `block` (sticky event parks
  it; recompute_ready will not touch it).
  ORDER IS LOAD-BEARING: block-first clears worker_pid -> unkilled zombie
  burning quota invisibly. Reclaim rc=1 ("not running") is a desired no-op.
- todo is safe once links exist (recompute_ready truth-gates; dispatcher
  spawns only ready) — no action needed for todo.
- unblock downstream when ALL its upstreams are done.
- reclaim running tasks stale > 2 h (kept from v1).
- Silent (exit 0, no output) unless it acted or the pipeline is wedged /
  finished (CRON NOISE rule).

Output (when non-silent) is delivered by cron to the manager.
"""
import subprocess
import sys
import json
import os
import time

BOARD = "bitblik-trust-demo"
# (upstream(s), downstream) — upstream may be a tuple for AND-deps
# Relay chain R1->R2->R3->R4->R5 (2026-08-14, demo today)
CHAIN = [
    (("t_d827c8c4",), "t_82c29a18"),   # R1 relay -> R2 signed envelope
    (("t_82c29a18",), "t_7d02f952"),   # R2 -> R3 demo --relay mode
    (("t_7d02f952",), "t_5e625d61"),   # R3 -> R4 presenter polish
    (("t_5e625d61",), "t_62270951"),   # R4 -> R5 Kimi cold review
]
FINAL = "t_62270951"
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
        print("PIPELINE COMPLETE: R5 cold review done. Board finished — notify operator with demo instructions.")
        return

    # Edge-triggered R3 ping (user asked): demo over ws://localhost the moment R3 lands
    r3 = by_id.get("t_7d02f952", {})
    ping_file = "/tmp/bitblik_r3_pinged"
    if r3.get("status") == "done" and not os.path.exists(ping_file):
        open(ping_file, "w").write(str(time.time()))
        print("PING for Felix: R3 LANDED — demo now talks over a real local Nostr relay (ws://localhost). "
              "Test: cd ~/repos/bitblik/demo/trust-proof && npx tsx src/demo.ts. "
              "R4 (polish) + R5 (Kimi review) still in flight.")

    # Self-heal links (idempotent INSERT OR IGNORE; duplicate-link rc != 0 is
    # fine). The gate must live in task_links, not just in this CHAIN config.
    for ups, down in CHAIN:
        for u in ups:
            kb("link", u, down)

    # Re-read AFTER healing links: `link` demotes a ready child with undone
    # parents to todo, and `block` only accepts running|ready — the gating
    # loop must act on post-link truth, not the pre-link snapshot.
    data, err = tasks()
    if data is None:
        print(f"WARN could not re-read board after link self-heal: {err}")
        return
    by_id = {t.get("id"): t for t in data if t.get("id")}

    actions = []
    for ups, down in CHAIN:
        up_states = [by_id.get(u, {}).get("status") for u in ups]
        down_s = by_id.get(down, {}).get("status")
        all_done = all(s == "done" for s in up_states)
        if all_done and down_s == "blocked":
            kb("unblock", down)
            actions.append(f"unblocked {down} (upstream(s) done: {', '.join(ups)})")
        elif not all_done and down_s in ("ready", "running"):
            if down_s == "running":
                # Fresh re-read: an upstream may have completed since the
                # snapshot — if all are done now this is a legitimate run,
                # hands off (do NOT block it).
                fresh, _ = tasks()
                fby = {t.get("id"): t for t in fresh or []}
                if all(fby.get(u, {}).get("status") == "done" for u in ups):
                    continue
                # reclaim FIRST (kills worker; rc=1 "not running" = desired
                # no-op), THEN block — order is load-bearing (see docstring).
                kb("reclaim", down, "--reason",
                   "watchdog: upstream(s) not done — premature spawn")
                actions.append(
                    f"RECLAIMED premature {down} (worker killed; upstreams: "
                    f"{dict(zip(ups, [fby.get(u, {}).get('status') for u in ups]))}) — "
                    f"premature spawn killed after <=2 min (quota burn bounded ~30-90 s)")
                down_s = "ready"   # reclaim leaves 'ready' — block below parks it
            kb("block", down, "watchdog: upstream(s) not done")
            actions.append(f"blocked {down} (gate: upstreams not all done: {dict(zip(ups, up_states))})")

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
