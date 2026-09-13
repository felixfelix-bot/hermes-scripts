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

### Parse contract for the gate fields (pinned)

`read_gate_state` emits four fields — `paused`, `quota`, `resume_at`,
`reason` — joined with **US (0x1f)**, and the dispatcher splits them back on
`US` with parameter expansion. Four rules are load-bearing and must not
drift:

1. **The delimiter must be a non-whitespace char.** A TAB does not work:
   bash treats tab as IFS *whitespace*, so consecutive delimiters collapse
   and a quota pause with `resume_at: null` (what `rate_limit_gate.py`
   emits on every `-503`/quota-window episode) silently shifted `reason`
   into `GATE_RESUME_AT` and left `GATE_REASON` empty. Fixed 2026-09-13
   (cross-family review finding; commit `975a121`, `t16` leg).
2. **`reason` must stay the LAST field.** The split folds every remaining
   field into the final variable, which is what makes a literal US (or tab)
   inside `reason` harmless. A fifth field must therefore be inserted
   *before* `reason`, never appended after it — otherwise the empty-field
   shift bug reopens for `resume_at`.
3. **The reader must not be line-based.** No `read -r a b c d <<< "$parsed"`
   (nor any `read` without an explicit non-newline `-d`): `read` stops at the
   first NEWLINE, so a `reason` carrying a decoded JSON `\n` — multi-line
   upstream error bodies, joined stack traces — was **truncated at that
   newline** in the board-pause marker and in the 2h/6h manager alerts.
   `reason` is *data*: newlines inside it are ordinary bytes. The current
   split uses parameter expansion on US (`${rest%%$'\x1f'*}` /
   `${rest#*$'\x1f'}`), which cannot stop early and still preserves empty
   fields. Fixed 2026-09-13 (kimi-family cold review of the T3.2 fix set,
   finding #8 — pre-existing; `t17`/`t18` legs). Producers are therefore NOT
   required to keep `reason` single-line.
4. **A malformed helper line fails open.** The inline helper always prints
   exactly four US-joined fields; if fewer than three US delimiters arrive,
   the output is treated as unparseable (dispatch proceeds, no markers) —
   the same rule as a missing/corrupt gate file. Never wedge dispatch on our
   own bug.


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
  never incremented — asserted with a pre-loaded non-zero counter in the
  test suite, not just at 0).
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

`bash tests/test_staggered_dispatch.sh` — 20 integration legs, 79
assertions: pause writes markers + skips dispatch (stub + real CLI, with a
claimable `ready` task left untouched so "no claim" is not vacuous), 429 /
QUOTA-WINDOW / KALMAN classified, unknown reason → no markers, auto-resume
removes markers, advisory + fail-open dispatch, `--sweep` re-queues a real
quota-paused task via the real CLI with a pre-loaded non-zero
`consecutive_failures` proving the re-queue never counts as a failure (control
`review-required:` task stays blocked), sweep no-op while paused, real-CLI
paused run leaves zero new task_runs, canary fires once after 6 h and not
before / not twice in a window, 2 h alert fires exactly once per episode,
marker preserves episode start across passes, a tab inside the reason does not
shift fields, an empty `resume_at` (null — what the gate emits) does not
shift fields either, a decoded `\n` inside the reason is not truncated
(`t17`), a multi-line reason with a null `resume_at` keeps both fields
(`t18`), a literal US (the delimiter itself) inside the reason is data
(`t19`), and a multi-line reason reaches the 2 h manager-alert text AND the
marker intact (`t20`) — the alert renders the reason verbatim, so it was
truncated pre-fix too. Legs are hermetic (no leg can spawn a real worker):
stub-hermes
asserts orchestration, `--sweep` and paused full-runs use the real CLI with a
`HERMES_KANBAN_DB` pin.

Implementation notes for operators:

- The sweeper resolves each board's DB as
  `$KANBAN_BOARDS_ROOT/<board>/kanban.db` (read-only) and calls unblock with
  that same path pinned via `HERMES_KANBAN_DB` (no `--board` flag) — the
  identical file `--board` resolves in production, but overridable for
  tests.
- `hermes kanban unblock` records the reason as a comment before flipping
  status, so every sweep leaves an audit trail on the task.
