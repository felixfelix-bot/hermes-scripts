# PROGRESS — t_00fa8726 fleet-scheduler re-block loop

Task: fleet_scheduler re-blocks the same cards with 'BLOCKED: fleet-offload:*' forever.
Worktree: /home/c03rad0r/worktrees/t_00fa8726 (branch worker-base/FLEET-SCHED-RECUR)
Repo: /home/c03rad0r/repos/hermes-scripts
Push remote: `felix` = github.com/felixfelix-bot/hermes-scripts (origin=c03rad0r/hermes-scripts
  403s for the active gh identity felixfelix-bot — push to `felix` instead).

## Recon (2026-10-10)
- Live file ~/.hermes/scripts/fleet_scheduler.py md5 94e01b36… was AHEAD of repo HEAD
  (a60a612) by ~310 lines (t_dd8ff7ca winner gating, _has_local_run, stale-freeze
  self-heal, DEATHPROOF prompt). Live file is the production code; repo copy was stale.
- Measured firehose: 2026-09-13 peak ~4000 comments/3h -> 672 per 48h, 549 distinct
  (board,task,winner) pairs, max 2 comments per pair. No >=3 repeats in 48h, so the
  *unbounded* 2.6-min loop no longer reproduces; the residual duplicate is real.
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
- SECOND GAP (DoD item 3): nothing consults the board DB before `hermes kanban block`,
  so a state purge/prune/restart (held=None) re-comments a card that already carries
  our recent hold for the same winner.

## Done
- [x] Commit 5ae7483: sync the live tree into the repo (prereq).
- [x] Commit 5b6c056: tests/test_fleet_scheduler_hold_loop.py — RED 3/8 with the fix
      reverted (6 BLOCKED comments / 24 ticks, duplicate comment same winner).
- [x] Commit 081c31c: fix — deny guard on every release path + board-DB dedupe
      (`_fleet_block_ts`, TTL-bounded, per-winner). 8/8 GREEN.
- [x] Pushed to `felix` (GitHub): ref == 081c31c06b5523b47b402e6b3a7f862fb07af73b.
- [x] Deployed to live: backup fleet_scheduler.py.bak-t00fa8726-<ts>, live md5 ==
      worktree md5 4ac9ed2e2d3deef26b5e21ee89c49adb, `systemctl --user restart
      fleet-scheduler.service` (new PID 3422740, ticks normal in the journal).
- [x] Regression: tests/test_review_lane_dispatch.py 5/5 PASS with
      HERMES_HOME=/home/c03rad0r/.hermes (needs the canonical home; the worker's
      profile-scoped HERMES_HOME makes it fail — pre-existing, unrelated).

## Remaining
- [ ] ngit mirror push (this repo has NO ngit remote — decision needed).
- [ ] Follow-up card: prune the ~670 historical noise comments (DoD item 4).
- [ ] REPORT.md + review.
