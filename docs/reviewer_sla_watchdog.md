# reviewer_sla_watchdog.py — reviewer-pool SLA watchdog (D-114)

Cron (every 30 min, script-only/no-agent): `~/.hermes/scripts/reviewer-sla-watchdog.sh`
wraps this script with `nice -n 19` / `ionice -c3` and a 120 s `timeout`, and
points it at the `merge-deputy` board:

```
python3 reviewer_sla_watchdog.py \
  --db             ~/.hermes/kanban/boards/merge-deputy/kanban.db \
  --state-file     ~/.hermes/profiles/manager/cron/output/reviewer_sla_state.json \
  --sla-hours 4 --politeness-hours 6 --max-reassign 1 \
  --halt-file      ~/.hermes/scripts/REVIEWER_HALT
```

Job id `d46b0c52b77f` (`reviewer-sla-watchdog`, `*/30 * * * *`, `deliver=local`).

**Silent watchdog contract:** stdout is EMPTY when there is nothing to report,
so a script-only cron tick delivers nothing. Non-empty stdout means an alert.

**Read-only on the board:** the DB is opened read-only (`mode=ro` URI, no
writable fallback). The watchdog never writes the kanban DB; a reassignment is
a *recommendation* printed to stdout — a human/operator owns execution.

## SLA definition

A task is stale when it is assigned to a reviewer profile
(`worker-reviewer-kimi` | `worker-reviewer-glm`), its `status` is `running` or
`review`, `started_at` is set, and `now - started_at > --sla-hours` (default
4 h). Sampling is `<= SLA/2` by design: the cron runs every 30 min against a
4 h SLA, so a breach is observed at most ~2 h after it opens. The 4 h
merge-queue digest cadence is far too coarse to enforce an SLA.

## Decision logic

| Condition | Decision | Consumes politeness window / reassign budget |
|---|---|---|
| stale, `reassign_count < --max-reassign` | propose cross-family reassignment (kimi <-> glm) | yes |
| stale, budget exhausted | route to MANAGER queue | no |
| `--halt-file` present and non-empty | HALT — never reassign, route to MANAGER | no |

Cross-family pairing is preserved: a reassignment proposal always names the
*opposite* family profile to the current assignee, so primary + cross-check
stay from complementary families on the same diff.

Manager-routed alerts (HALT / budget-exhausted) deliberately do NOT update
`last_alerted`, so a still-stale task is reconsidered the moment HALT lifts or
the reassign budget is re-enabled — no 6 h politeness stall after an operator
override.

## Shared ledger

`--state-file` is a persistent assignment ledger that survives watchdog
restarts. Per review task id it records:

```json
{
  "<task_id>": {"last_alerted": 1788184494, "reassign_count": 1}
}
```

`reassign_count` is the *reviewer-generation* and doubles as the dedup key
`(task_id, reassign_count)` against the 4 h merge-queue digest.
`~/.hermes/profiles/manager/scripts/merge_queue_digest.py` is the CONSUMER: it
reads the SAME ledger file, takes the same advisory `flock` at `<path>.lock`,
and reports watchdog-owned generations WITHOUT re-proposing them, so exactly
one actor proposes a given generation's reassignment.

`--max-reassign` (default 1) caps reassignment proposals per task; past the cap
the task routes to the MANAGER queue instead, so there are no kimi<->glm
ping-pong loops.

Ledger writes are atomic (temp file + `fsync` + `os.replace`) and the
load -> decide -> write critical section is held under the advisory file lock.
`--dry-run` reports without mutating the ledger.

## Exit codes

`0` = ran (whether or not anything was reported). `2` = board DB not found
(error on stderr). Corrupt state file: warns on stderr and continues.

## Tests

```
cd ~/.hermes/scripts && python3 -m pytest tests/test_reviewer_sla_watchdog.py -q
```

26 tests, 96% line coverage. Reviewer doctrine, lanes, standing
reviewer-repo assignments and the review-task scope discipline live in
`hermes-orchestration/docs/REVIEWER-POOL-RUNBOOK.md`.
