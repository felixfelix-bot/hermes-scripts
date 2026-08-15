# rate_limit_gate.py — dispatch-level rate-limit gate

Cron (every 5 min): `~/.hermes/bot/rate_limit_gate.py` writes the gate
decision to `~/.hermes/state/rate_limit_gate.json`. `staggered-dispatch.sh`
reads the file's `paused` key before every dispatch decision (fail-open when
the file is missing or unparseable). On a quota-class pause the dispatcher
additionally writes board-pause markers and re-queues `quota-paused:` tasks
once clear — see `docs/board-pause.md` (T3.2).

State shape:

```json
{
  "paused": false,
  "resume_at": null,
  "reason": "clear",
  "ts": "2026-08-15T10:45:01+00:00",
  "checked_at": "2026-08-15T10:45:01+00:00",
  "checks": {
    "peak_hour": {...}, "recent_429": {...}, "kalman": {...},
    "recent_503": {...}, "quota_windows": {...}
  }
}
```

Exit codes: 0 = clear, 1 = paused.

## Checks (decision priority, first match wins)

1. **recent_503** (T3.1, fail-closed) — ≥3 z.ai upstream 503/5xx within
   10 min → `paused=true`, top-level reason `zai-503-outage: ...` (the
   `zai-*` prefix is a contract — T3.2/T3.3 match on it),
   `resume_at = now + min(Retry-After, 20 min)`. Re-evaluated every run, so
   the pause naturally lifts once the burst ages out of the window.
   Sources (max confirmed count across sources, never summed):
   - `zai_usage.db anomaly_events`: `key_backoff` rows with
     `error_type=server` (the proxy records every upstream 500/502/503/504
     with a `backoff Ns` hint — the Retry-After proxy).
   - `zai_usage.db api_calls`: `status_code = 503` rows (future-proof).
   - `journalctl -u zai-proxy.service` (best-effort; unreadable journal →
     skipped, fail-open — the DB sources are primary).
2. **recent_429** — any 429 in the last 5 min → paused ~60 s (longer when a
   retry hint exists).
3. **quota_windows** (T3.1, advisory pause, fail-open) — any 5-hour /
   weekly / monthly window ≥85% used → paused with
   `resume_at = window resets_at` (fallback: re-check in 30 min when
   `resets_at` unknown; stale windows `age_s > 30 min` are skipped).
   Source: the proxy's own `/quota` cache at `http://localhost:9099/quota`
   (same source the dq05 monitor reads).
4. **kalman** — predicted quota exhaustion inside the task window.
5. **peak_hour** — advisory only, never pauses.

Fail-open rules: missing DB, unreachable quota collector, stale payload, or
unreadable journal → that check abstains. Only a *confirmed* 503 burst
(counted from real events) fails closed.

## Knobs

| Env var | Default | Purpose |
|---|---|---|
| `HERMES_GATE_STATE` | `~/.hermes/state/rate_limit_gate.json` | State file path (used by tests) |
| `HERMES_GATE_QUOTA_URL` | `http://localhost:9099/quota` | Quota collector endpoint |

Threshold constants live at the top of the script:
`RECENT_503_WINDOW` (600 s), `RECENT_503_THRESHOLD` (3), `MAX_503_RESUME_S`
(1200 s), `QUOTA_WINDOW_PCT` (85), `QUOTA_STALE_S` (1800),
`QUOTA_FALLBACK_RESUME_S` (1800).

## Tests

- `tests/test_rate_limit_gate.py` — T3.1 pure functions, synthetic sample
  sets (burst windows, retry caps, window thresholds, stale payloads).
- `tests/test_rate_limit_gate_existing.py` — pre-existing DB checks +
  run_gate wiring (in-memory SQLite, patched collectors).
- `tests/forced_live_gate_test.py` — fixture DB with a synthetic 503 burst
  + fixture quota server → asserts the written gate file pauses with the
  right resume_at, honors the ≥3 threshold, pauses on an 88% window, and
  fails open when the collector is unreachable.

Run: `python3 -m pytest tests/ -q` (43 tests) and
`python3 tests/forced_live_gate_test.py`.

## Deployment

`~/.hermes/scripts/rate_limit_gate.py` is the source of truth (this repo);
the cron copy is `~/.hermes/bot/rate_limit_gate.py`. After changing the
script, copy it to both locations — the bot copy is what cron executes.
