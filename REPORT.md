# Task report: t_7682f246

Commit: 9b93049755e5f40a0c53c4037584890d9cdc7dca
Branch: worker-base/t_7682f246
Remote: origin/worker-base/t_7682f246 (push observed)

Implemented safety controls

- `state_db_health.check_conn_ro()` now classifies every SQLite open failure as `unknown`, preserving `open failed: <cause>` detail.
- `check_conn()` now classifies any incomplete/exceptional `quick_check` as `unknown`; only returned non-`ok` quick-check rows classify `corrupted`.
- Auto-repair no longer independently reclassifies live stores. The health result only nominates a store. Before recovery, it creates an isolated SQLite snapshot and requires that snapshot's completed quick-check result to be `corrupted`.
- Recovery application refuses zero-message output unconditionally.
- Partial/lossy recovery requires explicit `allow_lossy`, an available positive incumbent message count, and at least `min_recovery_fraction` (default 50%) of incumbent messages. This protects against absolute-floor replacement of a large usable database by a tiny recovered one.

Tests and verification

- `python3 -m pytest -q tests/test_state_db_repair_safety.py`: 7 passed.
- `python3 -m py_compile state_db_health.py state_db_autorepair.py tests/test_state_db_repair_safety.py`: passed.
- Live-policy dry-run, with no apply: `HERMES_HOME=/home/c03rad0r/.hermes python3 state_db_autorepair.py --json --profile manager` returned no repair candidates and performed no action.
- Live guard run through the new classifier: `HERMES_HOME=/home/c03rad0r/.hermes python3 state_db_health.py --json` returned status `ok`, `corrupt: []`, `unknown: []`; all 11 readable stores were checked. The >256 MiB worker-base (665,571,328 bytes) and worker-reviewer-glm (930,598,912 bytes) were both `ok` via `ro_quick_check`.

No live database was deleted, modified, or replaced.
