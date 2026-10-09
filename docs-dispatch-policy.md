# Dispatch policy: resource gate, board list, hold scope

## What changed (2026-10-09)

`staggered-dispatch.sh` used a hardcoded `LOAD_THRESHOLD=3.4` compared against
**absolute** loadavg, plus an inline `free -m` check. Three defects:

1. **absolute, not per-CPU** — 3.4 means ~0.85/CPU on 4 cores and something else
   on 16; it cannot travel between machines;
2. **loadavg counts uninterruptible IO wait**, so a box doing disk work reads as
   loaded while its CPUs idle;
3. it was a **second gate disagreeing with the fleet's own policy**
   (`fleet.json`: `max_load_per_cpu` 0.8, `min_mem_available_mb` 1536).

Now one gate decides: `dispatch_gate.py`, thresholds and the board list in
`config/dispatch_policy.json`.

- **Memory and swap veto** (`min_mem_available_mb`, `max_swap_used_pct`). This is
  the failure mode we actually observed: workers were oomd-killed on memory
  pressure, not starved of CPU.
- **CPU throttles** (`max_load_per_cpu` × cores).
- The board list is read from the same file (`dispatch_gate.py --boards`), so
  adding a board is a config change, not a shell edit.

## Deployment (config as code)

`install-dispatch-policy.sh` installs `staggered-dispatch.sh`, `dispatch_gate.py`
and `config/dispatch_policy.json` into `~/.hermes/scripts/` **together**, then
runs the gate as a pre-flight. Shipping the script without the gate and config
wedges dispatch, so the three move as one unit.

**Deliberate choice: fail-CLOSED.** A missing/non-executable gate now raises an
`alert` and blocks dispatch, rather than logging quietly. Spawning unguarded
workers on a memory-starved host is what caused the oomd kills; a noisy stop is
the safer failure.

## Hold semantics — current state, stated honestly

`hold` in `fleet_boards.json` is a **local emergency switch for this node**. It
is not a fleet-wide brake: each node reads its own file.

`fleet_arbiter.py` emits remediation intents carrying a `target` node
(`{"target": node, "action": "hold"|"freeze"|"drain"|"quarantine", ...}`) and
writes a per-target lease. **No component consumes those intents today**
(verified 2026-10-09: no reader of the intent/lease channel). So:

- per-node hold semantics are **NOT enforced**; do not rely on them;
- the only hold that has effect is the local `fleet_boards.json` switch;
- `tests/test_fleet_hold_scope.py` covers the **local offload-hold path** only
  (`apply_holds`: peer winner ⇒ local copy parked). It does **not** cover
  arbiter intents, because there is no production behaviour to cover.

Wiring per-node intent consumption into the scheduler is a follow-up, not part
of this change. Until it lands, anything that reads a "global hold" claim is
reading a misdiagnosis.
