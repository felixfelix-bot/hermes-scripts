# PROGRESS — t_00fa8726 fleet-scheduler re-block loop

Task: fleet_scheduler re-blocks the same cards with 'BLOCKED: fleet-offload:*' forever.
Worktree: /home/c03rad0r/worktrees/t_00fa8726 (branch worker-base/FLEET-SCHED-RECUR)
Repo: /home/c03rad0r/repos/hermes-scripts (origin=c03rad0r/hermes-scripts)

## Recon (2026-10-10)
- Live file ~/.hermes/scripts/fleet_scheduler.py md5 94e01b36… is AHEAD of repo HEAD
  (a60a612) by ~310 lines (t_dd8ff7ca winner gating, _has_local_run, stale-freeze
  self-heal, DEATHPROOF prompt). Live file is the production code; repo copy is stale.
- Measured firehose: 2026-09-13 peak ~4000 comments/3h -> now 672 per 48h, 549
  distinct (board,task,winner) pairs, max 2 comments per pair. No >=3 repeats in 48h,
  so the *unbounded* 2.6-min loop no longer reproduces; the residual duplicate is real.
- FIELD PROOF (task_events, meshcore):
  * t_a8fcf378: blocked(fleet-offload:cobrador)@1791541710 -> unblocked@1791552087
    -> blocked(fleet-offload:cobrador)@1791552428 -> block_loop_detected(recurrences=2)
    -> card ARCHIVED@1791552565.
  * t_67f3b03e: blocked(cobrador)@1791541864 -> block_loop_detected -> promoted@1791590819
    -> blocked(cobrador)@1791591185 -> block_loop_detected(recurrences=3).
- ROOT CAUSE (provable): release_stale_holds() drops a hold WITHOUT installing the
  `hold_denied` guard whenever the card is not currently blocked/scheduled (the
  `_st not in _UNBLOCKABLE_STATUSES` branch) — contradicting its own docstring
  ("the next tick cannot immediately re-block the card we just freed"). apply_holds()
  then re-blocks on the next tick and the kernel writes a second BLOCKED comment.
- SECOND GAP (DoD item 3): nothing checks the board DB before `kanban block`, so a
  state purge/prune/restart (held=None) re-comments a card that is already blocked
  for the same winner.

## Plan
1. RED test tests/test_fleet_scheduler_hold_loop.py (3 cases).
2. Fix A: deny guard on every release path.
3. Fix B: `_blocked_for()` board-DB dedupe before `kanban block`.
4. Deploy the fixed file to ~/.hermes/scripts/fleet_scheduler.py (repo == live end state).
5. Push GitHub + ngit; REPORT.md.

## Log
- 17:1x recon done (above).
