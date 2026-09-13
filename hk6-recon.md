# HK-6 recon — classify both 15m governors, pick per-governor LLM path

**Task:** t_eaba6584 (board `house-keeping`) · **Date:** 2026-09-14 · **Author:** worker-routing
**Purpose:** decision memo consumed by the two conversion tasks (`t_e9f40dc5`, `t_661e140c`) and the verify task (`t_6672bf18`). **No cron was modified by this task.**

## 0. Where these crons actually live (read this first)

| Thing | Actual location |
|---|---|
| Cron definitions | `~/.hermes/profiles/manager/cron/jobs.json` (JSON, top-level keys `jobs` / `updated_at`) — **manager profile cron store** |
| Governor scripts (the wrapped python) | `~/.hermes/bot/dynamic_context_length_governor.py`, `~/.hermes/bot/compression_growth_governor.py` (git repo, remotes `felixfelix`/`dr`-style) |
| Cron `script` resolution root | `~/.hermes/profiles/manager/scripts/` (see pitfall #4 below) |
| This memo | `/home/c03rad0r/repos/hermes-scripts/hk6-recon.md` — the scripts repo (`origin` = c03rad0r/hermes-scripts, `felixfelix` = felixfelix-bot/hermes-scripts), per the task spec |

Not in scope / not found anywhere: root `crontab` has **no** entry for either governor (only `task-lifecycle-governor.py`, a different script); `hermes cron list` under the **worker-routing** profile prints `No scheduled jobs` because the cron store is **profile-scoped** — the jobs live under `manager`. `repos/hermes-scripts` contains no cron manifest for them.

## 1. Governor A — `dynamic-context-length-governor`

| Field | Value (live, read from `jobs.json` + last cron output) |
|---|---|
| cron name | `dynamic-context-length-governor` |
| job id | `4fbae8ad882c` |
| schedule | `35 3,8,18,23 * * *` (**4×/day — NOT `*/15`**) |
| workdir | `null` |
| agent flag | `no_agent: false` → **LLM agent path** |
| model / provider | `null` / `null` (profile default) |
| enabled_toolsets | `["terminal"]` |
| deliver | `local` (saved to `~/.hermes/profiles/manager/cron/output/4fbae8ad882c/`, never delivered) |
| wrapped script | `~/.hermes/bot/dynamic_context_length_governor.py` (23,655 B / 667 lines), invoked via `python3` in the prompt |
| side-effect state | `~/.hermes/bot/dynamic_context_state.json` (mtime 2026-09-14 03:36, i.e. written by the last run) |
| runs completed | `repeat.completed: 923` |
| enabled | `true`, `state: scheduled` |

**Prompt verbatim (stored, `prompt` field; the cron runner prepends its own delivery/silence preamble):**

```
QUOTA GATE FIRST: run ~/.hermes/profiles/manager/scripts/zai-quota-gate.sh; if exit 1, skip silently.

Run the dynamic context length governor:
python3 ~/.hermes/bot/dynamic_context_length_governor.py

This detects the actual active model from zai_usage.db and sets context_length to the maximum supported. Zero LLM cost. If detection fails, leaves config unchanged (backward compatible).
```

## 2. Governor B — `compression-growth-governor`

| Field | Value |
|---|---|
| cron name | `compression-growth-governor` |
| job id | `69648c5fb509` |
| schedule | `40 * * * *` (**hourly at :40 — NOT `*/15`**) |
| workdir | `null` |
| agent flag | `no_agent: false` → **LLM agent path** |
| enabled_toolsets | `["terminal"]`, `deliver: local` |
| wrapped script | `~/.hermes/bot/compression_growth_governor.py` (30,791 B / 768 lines) |
| side-effect state | `~/.hermes/bot/compression_growth_state.json` + `compression_growth_override.json` (mtime 2026-09-14 03:41) |
| runs completed | `repeat.completed: 1097` |
| enabled | `true`, `state: scheduled` |

**Prompt verbatim:**

```
QUOTA GATE FIRST: run ~/.hermes/profiles/manager/scripts/zai-quota-gate.sh; if exit 1, skip silently.

Run the compression growth governor (chains AFTER dynamic context length governor):
python3 ~/.hermes/bot/compression_growth_governor.py

This measures context growth rate from zai_usage.db using a 1-D Kalman filter and dynamically adjusts compression.threshold via hermes config set. Reads context_length dynamically from config.yaml (set by the dynamic context length governor). Zero LLM cost. If zai_usage.db missing, uses FALLBACK_THRESHOLD.
```

### Cadence correction (do not propagate the `*/15` claim)

The two conversion cards and the parent card say "preserve the `*/15` schedule". **Neither governor runs every 15m.** Live schedules are `35 3,8,18,23 * * *` (governor A) and `40 * * * *` (governor B). Two manager skill docs still assert 15-min cadence — both are stale and should be fixed by whoever touches the area:

- `~/.hermes/profiles/manager/skills/devops/compaction-tuning/references/dynamic-context-governors.md:6` — `**Cron:** every 15 min (4fbae8ad882c), runs before cost and growth governors`
- `~/.hermes/profiles/manager/skills/devops/ops-status-check/references/perpetual-alert-noise-three-root-causes.md:22` — `69648c5fb509 "compression-growth-governor" — 40 3,8,18,23 * * *`

Conversion tasks must **preserve the live cronspecs**, i.e. change only the `no_agent`/`script` fields.

## 3. STEP 2 — does the LLM prompt add reasoning the script does not do?

**Governor A: NO. Pure overhead.** The prompt is literally "run the script" plus a static description. The script (`main()`, line ~600-663) discovers profiles, detects the active model from `zai_usage.db`, calls `hermes config set context_length …`, persists `dynamic_context_state.json`, and prints a JSON aggregate (`print(json.dumps(aggregate, indent=2))`, line 662). Last two recorded agent responses: `[SILENT]`, `[SILENT]` — the agent does nothing but run the command and suppress output.

**Governor B: NO reasoning — only re-narration.** The prompt is "run the script and report its output". The recorded responses are a reformatting of the script's own JSON: `growth_rate`, `kalman_estimate`, `price_per_m`, per-profile `old→new threshold`, the `HYSTERESIS=0.02` deadband verdict, pressure integrator numbers. No decision, no cross-referencing, no prioritisation — the control action (`hermes config set compression.threshold`) happens inside the script. There is *one* soft value: the agent prose is currently the only human-readable record of *why* a threshold moved — but `deliver: local` means it is never delivered to anyone, and the same facts are already in `compression_growth_override.json` + the script's stdout JSON.

Both scripts are stdlib-only and self-contained ("zero LLM cost" as the prompts themselves state); the only LLM in the loop is the agent hop.

## 4. STEP 3 — control-loop / release-chain check (mandatory, from audit-1)

Command run:

```
grep -rn -e 'dynamic-context-length-governor' -e 'compression-growth-governor' \
         -e 'dynamic_context_length_governor' -e 'compression_growth_governor' \
         --include='*.sh' --include='*.py' \
         ~/.hermes/scripts ~/repos/hermes-scripts ~/.hermes/profiles/manager/scripts ~/.hermes/kanban
grep -rn -e '4fbae8ad882c' -e '69648c5fb509' --include='*.sh' --include='*.py' --include='*.md' <same dirs + manager/skills>
crontab -l | grep -i -e governor -e comp-gov -e context_length
```

Raw evidence lines (all hits in gate/release/unblock/dispatch/monitor territory):

```
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:492  # 6b. Compression-governor staleness (2026-09-02, plan A4)
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:494  #     and nobody noticed for 10 days. Both governors write state on every
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:497  check_governor_staleness() {
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:506      "$HOME/.hermes/bot/compression_governor_state.json" \
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:507      "$HOME/.hermes/bot/compression_growth_state.json"; do
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:518        add_alert "GOVERNOR" "$(basename "$state_file") is $((age / 60)) min stale — compression governor chain broken (cost governor pid: comp-gov hermes-cron job / growth gov 69648c5fb509)" "high"
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:521        add_alert "GOVERNOR" "$(basename "$state_file") missing — compression governor never ran or state was deleted" "medium"
~/.hermes/profiles/manager/scripts/unified-system-alert.sh:1143   check_governor_staleness
~/repos/hermes-scripts/state-size-canary.py:170  # Operator-pinned context lengths (mirror of the ctx-governor's
~/repos/hermes-scripts/state-size-canary.py:241  #    the independent watcher — it does NOT trust the governor.
~/repos/hermes-scripts/state-size-canary.py:240  #    to 200000 repeatedly. The governor now heals drift, this canary is
```

Negative results (grep returned nothing for either governor name or either cron id):

- `repos/hermes-scripts/gate_engine.py`, `gate_tick.py` — no hits.
- `repos/hermes-scripts/hk-bootstrap-release.sh` — no hits.
- Any release / unblock / dispatch script (`~/.hermes/scripts`, `manager/scripts`, kanban scanner/reaper scripts) — no hits. `crontab` — no hits.
- Only remaining hits repo-wide are the governor scripts themselves, their tests, and design/plan docs.

**Verdict:** neither governor is in a **release or unblock chain**. The single external coupling is `unified-system-alert.sh:check_governor_staleness()` (an *alert monitor*, not a gate), and it reads the **state files** — files written by the **python script**, not by the LLM. `state-size-canary.py` independently cross-checks the governor's *config writes* by reading `config.yaml`; it is unaffected by the cron's transport.

## 5. STEP 4 — decisions

### (A) `dynamic-context-length-governor` → **DECISION A: no_agent script-only cron**

Justification: the prompt adds zero reasoning (every recorded response is `[SILENT]`; the script's JSON aggregate is the whole output), and the governor is not in any release/unblock chain — the only monitor reads state files the script writes.

### (B) `compression-growth-governor` → **DECISION A: no_agent script-only cron**

Justification: the prompt is "run the script and report its output"; the agent only reformats the script's own JSON, the control action is inside the script, and the one monitor (`unified-system-alert.sh`) depends on `compression_growth_state.json`, which the script keeps writing under `no_agent`. Breach handling is deterministic (a threshold was applied or it wasn't), so no LLM reasoning is needed either — i.e. pattern **B** is not required.

Decision **C** is not applicable to either: neither is control-loop/release-chain adjacent *in the LLM path*, and both prompts **already carry the quota-gate header** (`QUOTA GATE FIRST: run ~/.hermes/profiles/manager/scripts/zai-quota-gate.sh; if exit 1, skip silently.`) — so pattern C would be a no-op.

## 6. Required companion changes for the conversion (both governors)

1. **Silent-by-default stdout is mandatory before flipping `no_agent`** — under `no_agent`, non-empty stdout is delivered verbatim and empty stdout is silent. Both scripts currently print unconditionally:
   - `dynamic_context_length_governor.py`: `print(json.dumps(aggregate, indent=2))` at line 662, plus progress lines to stdout at lines 502, 507, 566, 575, 578, 619, 646, 654 (e.g. `[ctx-governor] {profile}: pinned …, ok`).
   - `compression_growth_governor.py`: `print(json.dumps(summary, indent=2))` at line 763 (most other diagnostics already go to `file=sys.stderr`, e.g. line 701 `updated {profile}: threshold …`).
   Add a `--quiet` mode (healthy/no-op run → empty stdout; alert line only on a real breach/apply) without changing alert content.
2. **Edge-triggered alert contract to preserve:** governor A emits only when it *applied* a `context_length` change (or healed a 413 regression); governor B emits only when `applied: true` for ≥1 profile (i.e. a `hermes config set compression.threshold` past the `HYSTERESIS=0.02` deadband). Healthy runs must be silent.
3. **Do not change the cronspecs** (`35 3,8,18,23 * * *` / `40 * * * *`) — only `no_agent` + `script`.
4. **Pitfall — `script` is resolved as a FILENAME under `~/.hermes/profiles/manager/scripts/`, not as a shell command line.** Per the header of `manager/scripts/comp-gov-run.sh` (created 2026-09-02): the comp-gov job had a full command line in its `script` field and every run since 2026-08-23 14:03 failed with `Script not found`. The fix was a wrapper file. So the conversion should add two tiny wrappers (e.g. `manager/scripts/ctx-gov-run.sh` → `exec python3 "$HOME/.hermes/bot/dynamic_context_length_governor.py" "$@"`, and the same for the growth governor with its quiet flag) and point `script` at the wrapper, rather than embedding `python3 …` in `script`.
5. **Monitor staleness comment is now stale:** `unified-system-alert.sh:500-504` documents governor B as "runs 4x/day → max ~10h gap → `max_age=43200`". Governor B is live at hourly (`40 * * * *`), so the 12h threshold is conservative and still safe; the comment and the hardcoded cron id inside the alert string at line 518 should be corrected in the same pass.
6. **Token delta:** expected saving ≈ 1 LLM session per run per governor — governor A 4 sessions/day × ~3-8K tokens, governor B 24 sessions/day × ~3-8K tokens ≈ **~28 sessions/day, ~100-220K tokens/day** removed. (Live counters at recon time: 923 + 1097 completed runs; the parent card's "627+630" is stale.) Log the delta in `hk-skip-counter.log` format as the parent card requires (no such file exists yet — neither in `~/.hermes/bot/` nor in `repos/hermes-scripts/`).
