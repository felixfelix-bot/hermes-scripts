# Disk cleanup — 2026-09-12 (kanban t_f978b464)

Host: CobradorWave (T470) · root fs `233G` on `/dev/nvme0n1p2`
Trigger: `/` at **93%** (17G free), threshold 85%; disk alert + Kalman disk prediction CRITICAL.
Previous passes: t_2c530663 (2026-09-09, 88%→87%), t_dcee015c (2026-09-10, 87%→83%).

## Result

| | before | after | delta |
|---|---|---|---|
| `df -h /` | 205G used · **17G free · 93%** | 188G used · **33G free · 86%** | **+16G freed (−7 points)** |
| `/tmp` (tmpfs, = RAM) | 2.0G / 3.6G (56%) | 247M / 3.6G (7%) | +1.8G RAM freed |

Tier: **safe tier only** (regenerable caches / build artifacts / temp cruft). No sudo used.
Circuit breaker: not triggered — no deletion failure, no du/df timeout.

## Deleted (safe tier, measured with du; verified by df delta)

| Item | Size | Notes |
|---|---|---|
| Rust `target/` dirs (5) | **11.0G** | ngit-ci-daemon-scope 5.7G, ngit-ci-filter-batching 2.1G, microfips-upstream 2.1G, ngitwf-tollgate-rs 1.8G, block-buzz 359M. Pre-checked: 0 cargo/rustc procs, 0 open fds |
| `~/.gocache-t9fbe` | 2.1G | orphaned agent-created Go build cache |
| `~/.bun/install/cache` | 1.5G | |
| `~/.cache/ms-playwright` | 641M | |
| `~/.cache/uv` | 316M | |
| `~/.npm/_cacache` | 258M | |
| `~/.cache/opencode` | 137M | cache only — live `opencode.db` untouched |
| `/tmp` stale dirs + loose files | ~1.75G | market-pr1292 1.1G, act-src 116M, gocache 71M, ndaemon-verify-phase3 46M, venvs 87M, dwarf/rdi logs 190M, clones 80M, 674 stale loose files |
| `~/.cargo/registry/cache`, `~/go/pkg/mod` (modcache) | 71M + 1.1G | Go modcache dirs are mode-0555 → `chmod -R u+w` needed first |
| git `tmp_pack_*` (3, orphaned pack files) | 49M | tg-rs-upstream 2×, tollgate-module-basic-go 1× |
| misc caches: pre-commit 36M, typescript 17M, Espressif 13M, pip 11M, virtualenv 11M, Trash, signal-tmp, rustup tmp, gopls/goimports/deno | ~120M | |

## Surfaced for Felix (needs-decision — NOT touched)

Ranked by reclaim value:

1. **node_modules in worktrees/projects — 45 dirs ≥500M = 43G** (30 dirs / 29.7G in `~/worktrees`, 5 / 4.9G in `~/repos`, 2 / 2.1G in `~/reviews`, 8 / 8.3G in `~` project dirs). Each plebeian-market install is ~1.0G. Regenerable (`bun install`), but 4 worktrees belong to *active* tasks (t_552e6cfc, t_d0898ee8, t_97ac0533-e2e-gate, ...). **Needs a policy call + guarded reaper, not a blind sweep.**
2. **`~/.rustup` 9.2G** — 6 toolchains (stable 1.9G, esp 1.9G, esp-xtensa 1.9G, nightly 1.8G, 1.95.0 1.4G, 1.94.1 599M). Old pinned 1.94.1/1.95.0 removable if no build pins them.
3. **PlatformIO 6.5G** (`~/.platformio` 4.8G + `~/.platformio-b` 1.7G) + **`~/.espressif` 4.0G** — re-downloadable toolchains/caches for embedded work; delete only if the balloon/meshcard track is idle.
4. **Docker (5.6G)** — unused tagged images: `openwrt/sdk:mediatek-filogic-v25.12.5` 2.09G + `openwrt/sdk:aarch64_cortex-a53-v25.12.5` 2.11G (pulled since the 09-10 sweep), `hermes-agent-harness:latest` 2.78G (41h old), and 29 anonymous dangling volumes 1.38G created 2026-09-11 05:42–05:46 (<48h → held per skill rule).
5. **`~/.local/share/opencode/opencode.db` 2.8G** — live DB, no retention by design; reclaim = session delete + VACUUM (skill `references/opencode-db-maintenance.md`). Requires no DB opener.
6. **Hermes profile state DBs — ~4G total** (`worker-base`/`worker-stackstr`/`worker-stackstr-qa` 663M each, `worker-admin` 654M, ...). Reclaim path = close+prune+optimize sessions (needs the profile's gateway stopped). Bot DBs: `zai_usage.db` 634M, `burn_attribution.db` 456M, `~/.hermes/backups/zai_usage-pre-fix2-*.db` 183M.
7. **`~/.tmp` 998M** — node-compile-cache 126M, opencode 74M, gh-cli-cache 40M, `zai_t4_4ter1iy7/snapshot.db` 456M, hashed scratch dirs. Regenerable, but outside `/tmp` so not in the skill's safe list.
8. **`~/ci-probe-t2454` 4.4G** (incl. its own `gocache/`) — CI probe checkout owned by active card t_2454e0b9.
9. `/tmp/tg-rel-x` 19M residual, root-owned (`tollgate.8`) — `rm` denied, no sudo used → **exempted, left in place**.
10. `~/snap/firefox` 3.9G (profile data), snap disabled `firefox 154.0-1` revision (needs sudo), `~/Downloads/FPGAs_..._Lin64.bin` 363M (Jun 4), journal 302M (**deliberately not vacuumed** — forensic value per card).

### Sparse-file caveat
`~/.config/nak/events/data.mdb` shows 274G **apparent** size but only 116K allocated (LMDB preallocated map). `find -size`/`du -b` overstate; no action needed — do not "clean" it.

## Regrowth since the 2026-09-09 / 09-10 passes (the recurring trigger)

| Regrew path | 09-10 cleanup | 09-12 measured | Δ |
|---|---|---|---|
| bun install cache | 1.2G deleted | 1.5G | **regrew +0.3G in 48h** |
| go modcache | 1.6G deleted | 1.1G | regrew |
| playwright cache | deleted | 641M | regrew |
| uv / pip / typescript / opencode / cargo-registry / npm caches | deleted | 316/11/17/137/71/258M | all regrew |
| Rust `target/` dirs | not in scope | **11.0G** (all mtimes 2026-09-12 10:42–15:05) | rebuilt within hours |
| docker `openwrt/sdk:*` images | 903M deleted | 4.2G (2 newer tags) | re-pulled at 4× size |
| anonymous docker volumes | tgsdk 0B | 29 vols / 1.38G (created 09-11 05:4x) | new |

**Root cause of the treadmill (3 mechanisms):**
1. **Per-task worktrees are never stripped of build trees.** 15G `~/worktrees` + 19G `~/repos` are roughly half build artifacts (43G of node_modules across projects, 11G of `target/`, 7.6G `.pio`, 9.2G `build/`, 2G `.venv`). Every new task creates a fresh worktree and installs/builds again — nothing reclaims them when the task ends. This — not caches — is what refills the disk in 48–72h.
2. **Tool caches regrow at ~2–3G/day of active agent work** (bun/playwright/uv/go/cargo/opencode). Deleting them buys 2–3 days, exactly matching the observed recurrence cadence.
3. **No reaper for build-spike docker images/volumes** — each spike leaves 1–8G (`openwrt/sdk` 4.2G, harness 2.8G, 1.4G of anon volumes).

**Proposed durable fix (follow-up card filed):** guarded nightly/weekly reaper that deletes `target/`, `node_modules/`, `.pio/` **only in worktrees with no active kanban task** (checked against every board DB) and older than N days, plus the safe-tier cache sweep; report df delta. Policy question for Felix first: is pruning `node_modules` in stale worktrees acceptable?

## Method / guardrails used
- `disk-cleanup` skill safe-tier list followed exactly; every deletion preceded by `lsof +D` live-writer check.
- Rust `target/` sweep: refused when any `cargo`/`rustc` process is running (none were: 0 procs, 0 open fds, load avg 2.9).
- No `sudo` anywhere; root-owned residue left in place and reported.
- Commands executed via script files (no giant inline one-liners); deletions via scripted `rm -rf`/`shutil.rmtree` with per-item error capture — nothing force-deleted on error.
- Companion script: `ops/disk-cleanup/safe-tier-cleanup.sh` (idempotent, `--dry-run` supported, same guards).
