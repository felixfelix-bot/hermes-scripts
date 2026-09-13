# ctx-hygiene — nightly stale context-card check

Nightly scan of the three active project kanban boards (admin, market, tollgate)
for open cards older than 7 days.

- `ctx_hygiene_check.py` — per-card listing (id, status, age, title) per board.
- `ctx_hygiene_summary.py` — per-board rollup: open / stale>7d / >30d / >60d, stale %,
  stale by status and assignee.
- `verify_ctx_hygiene.py` — ad-hoc verification: builds a synthetic kanban fixture
  (known truth) in a throwaway `$HOME` and cross-checks live output against an
  independent `sqlite3` oracle.

Both scripts read board DBs directly (`~/.hermes/kanban/boards/<slug>/kanban.db`)
because `hermes kanban ls` returns a global view, not board-scoped data.
Cron wrapper must gate first on `~/.hermes/bot/zai_state.json`
(`throttle` / `quota_pause` / `token_pct >= 80` → skip silently).
