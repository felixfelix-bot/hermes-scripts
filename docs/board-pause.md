# board-pause + quota sweeper — T3.2 dispatcher semantics

`staggered-dispatch.sh` (cron every 2 min) extends the T3.1 gate contract
with quota-aware pause/resume so a z.ai quota/outage event freezes dispatch
WITHOUT burning retry budgets, and everything recovers automatically when
the gate clears.

Related: `docs/rate_limit_gate.md` (T3.1 — the gate that produces the state
file this dispatcher consumes).

## Reason contract (pinned)

The gate's top-level `reason` for `paused=true` is always one of exactly
four prefixes (see `decide()` in `rate_limit_gate.py`):

| Prefix | Check |
|---|---|
| `zai-503-outage: …` | recent_503 burst (fail-closed) |
| `ACTIVE 429: …` | recent_429 |
| `QUOTA-WINDOW: …` | quota_windows ≥ 85% used |
| `KALMAN: …` | kalman exhaustion prediction |

All four are **quota-class**: they trigger board-pause markers. A paused
gate with any OTHER reason still skips dispatch (fail-closed on any pause)
but writes no markers and sweeps nothing — an unknown reason is a contract
violation you should be able to see in the logs.

`ADVISORY: …` is `paused=false` → dispatch proceeds (peak-hour caution only).

## Behavior

### Pause (quota-class gate pause)

For every board in `$BOARDS` the dispatcher writes
`$STATE_DIR/board_pause_<board>` (JSON):

```json
{
  "board": "hermes-for-friends",
  "paused_at_epoch": 1786793000.0,
  "paused_at": "2026-08-15T17:03:20Z",
  "updated_at_epoch": 1786793120.0,
  "reason": "zai-503-outage: 4 server errors in 600s",
  "resume_at": "2026-08-15T17:20:00+00:00",
  "alerted_2h": false,
  "canary_at_epoch": null,
  "canary_count": 0
}
```

and **skips the board entirely** — no `hermes kanban dispatch` call at all,
so no claims, no promotes, no failure accounting. Tasks sit `ready`, retry
budgets untouched. `paused_at_*` are preserved across passes (episode
start); `reason`/`resume_at` track the latest gate file.

Known race (bounded, safe): if the dispatcher is down while the gate clears
and re-pauses, the marker inherits the old episode's age — worst case an
early 2 h alert or one extra canary claim.

### Auto-resume

First pass with a confirmed-clear gate removes all managed boards' markers
(logs `board-pause marker removed board=… — auto-resume`).

### Manager alerts

- Pause episode older than **2 h** → one-time `ALERT board-paused >2h: …`
  (once per episode, tracked via `alerted_2h`).
- Every alert goes to syslog (`logger -t staggered-dispatch`), stderr, and
  stdout — the cron redirect lands it in `staggered-dispatch.log`.

### 6 h fail-safe (kill switch for a stale gate)

A pause episode older than **6 h** forces **one canary claim** — a single
`hermes kanban --board <first-paused-board> dispatch --max 1` — to probe
whether the gate is stale (if the outage is really over, the canary task
completes normally; if not, the canary worker sees the same outage and
blocks `quota-paused:` per the T3.3 taxonomy). The canary repeats at most
every 6 h, globally across boards, and alerts each time
(`ALERT BOARD-PAUSE FAILSAFE: …`).

### Quota sweeper (`--sweep` mode, and automatically on clear passes)

Tasks blocked with block-reason prefix `quota-paused:` (the T3.3 worker
taxonomy — workers block this way when they detect quota exhaustion) are
re-queued once the gate is confirmed clear:

- Match: latest `blocked`/`unblocked` event is `blocked` AND its payload
  `reason` starts with `quota-paused:` AND task status is `blocked`.
- Re-queue goes through `hermes kanban unblock <id> --reason "quota-sweeper
  (T3.2): gate clear …"` — the sanctioned CLI path (events + comments +
  parent re-gating) — which returns the task to `ready` and does NOT count
  as a failure (`consecutive_failures` is reset to 0 by `unblock_task`,
  never incremented).
- Sweeping only happens on a **confirmed clear** gate. A missing/unparseable
  gate file is fail-open for *dispatch* (unchanged) but does NOT sweep —
  re-queueing requires positive evidence the gate is clear.

Standalone: `staggered-dispatch.sh --sweep` runs only the sweeper (skips
marker management and dispatch) — safe to call manually.

## Configuration (env overrides)

| Var | Default | Meaning |
|---|---|---|
| `STATE_DIR` | `~/.hermes/state` | marker + gate state dir |
| `GATE_FILE` | `$STATE_DIR/rate_limit_gate.json` | T3.1 gate state |
| `BOARDS` | `fips infrastructure hermes-for-friends` | managed boards |
| `KANBAN_BOARDS_ROOT` | `~/.hermes/kanban/boards` | board DB root for sweeper queries |
| `QUOTA_REASON_PREFIXES` | `zai-:ACTIVE 429:QUOTA-WINDOW:KALMAN` | colon-separated quota-class reason prefixes |
| `PAUSE_ALERT_AFTER_S` | `7200` | manager-alert age threshold |
| `PAUSE_FAILSAFE_S` | `21600` | canary age threshold |
| `CANARY_INTERVAL_S` | `21600` | min seconds between canaries (global) |
| `HERMES_BIN` | venv `hermes` | binary for dispatch/unblock |
| `LOAD_THRESHOLD`, `RAM_MIN_MB`, `SLEEP_BETWEEN`, `FAILURE_LIMIT`, `LOCK_FILE` | unchanged | pre-existing knobs |

## Tests

`bash tests/test_staggered_dispatch.sh` — 14 integration legs, 51
assertions: pause writes markers + skips dispatch (stub + real CLI), 429 /
QUOTA-WINDOW / KALMAN classified, unknown reason → no markers, auto-resume
removes markers, advisory + fail-open dispatch, `--sweep` re-queues a real
quota-paused task via the real CLI with failures unchanged (control
`review-required:` task stays blocked), sweep no-op while paused, real-CLI
paused run leaves zero new task_runs, canary fires once after 6 h and not
before / not twice in a window, 2 h alert fires exactly once per episode,
marker preserves episode start across passes. Legs are hermetic (no leg can
spawn a real worker): stub-hermes asserts orchestration, `--sweep` and
paused full-runs use the real CLI with a `HERMES_KANBAN_DB` pin.

Implementation notes for operators:

- The sweeper resolves each board's DB as
  `$KANBAN_BOARDS_ROOT/<board>/kanban.db` (read-only) and calls unblock with
  that same path pinned via `HERMES_KANBAN_DB` (no `--board` flag) — the
  identical file `--board` resolves in production, but overridable for
  tests.
- `hermes kanban unblock` records the reason as a comment before flipping
  status, so every sweep leaves an audit trail on the task.
