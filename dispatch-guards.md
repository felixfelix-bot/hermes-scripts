# Dispatch Guards — staggered dispatch + D5 wake gates

Reference for the guard layer landed via `feat/dispatch-guards-2026-08-15`
(merged to master in 02dba7a). Covers what each guard does, its state files,
and its fail-open/fail-closed contract. See `rate_limit_gate.md` for the
upstream rate-limit gate that feeds these.

## staggered-dispatch.sh

Gated staggered dispatch (runs every 2 min off-peak, `flock`-protected).
Guards, in evaluation order:

1. **Load + RAM pre-check** — a pass is skipped entirely when load ≥
   threshold or available RAM is too low.
2. **Rate-limit gate** (`rate_limit_gate.json`, see rate_limit_gate.md):
   `paused=true` with a quota-class reason (`zai-*`, ACTIVE 429,
   QUOTA-WINDOW, KALMAN) writes `$STATE_DIR/board_pause_<board>` markers for
   every managed board and skips it completely — no claims, no promotes, no
   failure accounting. First clear pass auto-resumes (markers removed).
   *Fail-open:* missing/unparseable gate file → dispatch proceeds.
   *Fail-closed:* any `paused=true` → skip.
3. **Board-pause episode semantics** — alert to the manager when a pause
   episode exceeds 2 h (once per episode); after 6 h a fail-safe forces ONE
   canary claim (`--max 1`) so a stale gate can never freeze a board
   forever (repeats at most every 6 h).
4. **Quota-paused sweeper** (`--sweep`, also automatic on a clear pass) —
   tasks blocked with reason prefix `quota-paused:` are unblocked back to
   ready once the gate is confirmed clear; not counted as failures.
5. **Dispatch freeze marker** — emergency stop blocks ALL dispatch (SOUL
   contract).
6. **Circuit breaker** — `circuit-breaker.sh check <board>`; a HOLD skips
   the board. A breaker failure (rc 127 etc.) is fail-open: never wedge
   dispatch on the guard's own bugs.

Known bounded race: if the dispatcher is down while the gate clears and
re-pauses, the pause marker inherits the old episode age (worst case an
early alert or one extra canary — both safe).

Marker schema (`$STATE_DIR/board_pause_<board>`):
`{board, paused_at_epoch, paused_at, updated_at_epoch, reason, resume_at,
alerted_2h, canary_at_epoch, canary_count}`.

## kanban-assigner-gate.py — D5 wake gate

Pre-script for the kanban-auto-assigner cron. Scheduler contract: last
stdout line `{"wakeAgent": false}` + exit 0 skips the LLM session entirely
(zero tokens, `[SILENT]` preserved).

SLEEP (no LLM) when any of: z.ai quota exhausted for both keys
(`~/.hermes/bot/zai_state.json`), no ready+unassigned tasks on any board,
no idle worker profile, Kalman pool at capacity. WAKE only when an
assignment is genuinely possible; the scan summary is printed as agent
context. Missing state file ⇒ assume OK (fail-open).

## vps-watchdog-gate.py — D5 wake gate

Pre-script for the "VPS watchdog auto-fix (Tier 1)" cron. The watchdog
writes `~/.local/state/vps-dual-watchdog/alert.json` ONLY on failure and
deletes it when healthy, so the decision is deterministic: SLEEP when
quota-exhausted or no active failures; WAKE with the (≤20) failures as
starting context. Same scheduler contract as above.

## Auxiliary

- `proxy-restart-tonight.sh` — one-shot off-peak proxy restart helper
  (exports `XDG_RUNTIME_DIR` so `systemctl --user` works from cron).
- `.gitignore` — tracked; build artifacts (`__pycache__/`, `*.pyc`,
  `.coverage`, `.pytest_cache/`) ignored, with `!.secret-patterns.txt`
  negation so the tracked secret-pattern list stays visible to git.
- `code_index.py` symlink dropped (pointed outside the repo; deploy
  artifact).

## Verification

- Full suite: `python3 -m pytest tests/ -q` → 95 passed @ master (02dba7a).
- Cross-family cold review (G2.5, kimi-k3): VERDICT GO — 0 blocking,
  4 minor, 3 nits. Minors tracked as follow-ups: /tmp fallback lock race,
  absolute paths for `awk`/`free` in cron env, ImportError guard in
  kanban-assigner-gate.py, invalid default key handling in zai_monitor.py.
