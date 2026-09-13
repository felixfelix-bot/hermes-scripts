# worker quota-pause taxonomy — T3.3

Design + apply record for the `quota-paused:` worker taxonomy patch
(`PLAN-v2-remediation.md` §T3.3, decision **D9=B**: *the worker drafts a patch
file, the manager applies and commits it*).

**Status: drafted, verified, NOT applied anywhere.** No file outside this repo
(worktree `~/worktrees/t_87e5657d`, branch `hermes-v2/worker-quota-taxonomy`)
was modified by the drafting run. The manager applies it — see
[Apply procedure](#apply-procedure-manager).

## Deliverables

| File | What it is |
|---|---|
| `patches/kanban-worker_quota-pause-taxonomy.patch` | the `-p1` patch: version bump + the taxonomy section, anchored to apply to every current fleet variant |
| `patches/apply-kanban-worker_quota-pause-taxonomy.sh` | apply helper: base preflight, backup, `--fuzz=0` apply, marker verification, restore-on-failure, idempotent, `--dry-run`/`--force` |
| `tests/test_kanban_worker_quota_taxonomy.sh` | 42-assertion gate suite (patch shape, apply matrix, markers, canonical text, frontmatter, reversibility, negative control, helper behaviour incl. partial-state refusal and `.rej` hygiene) |
| `tests/verify_skill_loads.py` | runtime check: the patched skill is still discoverable by Hermes' manifest scanner |
| `tests/fixtures/kanban-worker-anchor-{A,B}.md` | hermetic, exact-context reductions of the two dominant fleet variants |

## The contract this implements (names are pinned — T3.1/T3.2)

The section tells a worker how to tell a **z.ai quota/outage** from a **real
task failure**, so an outage stops burning retry budget and context:

- **Signature (all four):** failure at the *first* LLM call; HTTP **503** (or
  429) mentioning quota/capacity/upstream from `localhost:9099` (the z.ai proxy)
  or the routstr upstream; the gate file says `paused` with a **quota-class**
  reason; the failure is **identical across unrelated steps**.
- **Action:** `kanban_block(reason="quota-paused: gate says <reason>, resume_at
  <ts> — no work lost, not a task defect")` **early (≤10 iterations)**, never
  `kanban_complete`, never grind to the iteration cap.
- **Real failure:** task-specific (fails at different steps, names concrete
  files/hosts/logic) or non-LLM (SSH/DNS/docker/permission/test/401, or the
  dispatcher's pre-flight load/RAM gate) → normal diagnose → fix → block flow.
- **Tie-breaker:** still unsure after 2 probes spaced ~10 min **and the gate is
  clear** → treat as a real failure.

Cross-artifact contract (must stay in sync with the dispatcher side):

| Contract | Where it is defined | This patch |
|---|---|---|
| block-reason prefix `quota-paused:` matched by `reason.startswith("quota-paused:")` | `staggered-dispatch.sh` (T3.2 sweeper, `docs/board-pause.md`) | quoted verbatim in the skill text; test leg t8/t9 asserts the literal |
| gate file `~/.hermes/state/rate_limit_gate.json` with `paused` / `reason` / `resume_at` | `rate_limit_gate.py`, `docs/rate_limit_gate.md` (T3.1) | documented + a copy-pasteable read command |
| quota-class reason prefixes `zai-503-outage:` / `ACTIVE 429:` / `QUOTA-WINDOW:` / `KALMAN:` | `docs/board-pause.md` (`QUOTA_REASON_PREFIXES`) | all four listed |
| 6 h canary + board-pause markers + free respawn after sweep | `docs/board-pause.md` | summarised in "what happens next" |

**Spec deviation, declared:** the T3.3 spec text phrases the gate condition as
"paused with a `zai-*` reason". The patch lists **all four quota-class prefixes**
(the plan's `zai-*` shorthand is called out explicitly in the table) because
T3.2 pauses, marks and sweeps on all four identically — a worker that blocked
only on `zai-503-outage:` would grind through a `QUOTA-WINDOW:` pause instead of
blocking. Widening is the conservative direction: every listed reason makes the
dispatcher skip the board anyway.

## Fleet reality (why the apply matrix matters)

79 `kanban-worker/SKILL.md` copies exist on this host. They are **real files,
not symlinks**, and Hermes resolves the **profile-local** copy first — so the
copy a worker actually reads is its own, not the manager's. Inventory taken
2026-09-13 (re-derive with the recipe below):

| md5 | copies | generation | view |
|---|---|---|---|
| `f93c72fa…` | **71** | **base A** — default profile (`~/.hermes/skills`) + every worker profile except the 7 below | the copy most dispatched workers read; still has the pre-Sep-11 `review-required:` block guidance |
| `8b0a8bba…` | 1 | **base B** — manager SoT (`~/.hermes/profiles/manager/skills`, git: `felixfelix-bot/hermes-manager-skills`) | review-required/operator-action + quality-gates additions |
| `1ff66cfc…` `f450c016…` `e963cf9b…` `0f5cf099…` `a0269405…` `8e22ed9b…` | 6 | stale one-off snapshots (worker-wizard, worker-admin, worker-tollgate, market-chore-applesauce-foundation, worker-base, worker-bitblik) | all six are still **base-A compatible** — this patch applies `--fuzz=0` to them unchanged |
| `cf883f4e…` | 1 | **newer generation** — `worker-plebeian` only: already `version: 2.1.0`, carries the 2026-09-11 `kanban_request_review` guidance | needs re-anchoring (see below) |

Verified apply matrix, `--fuzz=0` (all 79 copies tried):

- ✅ 78/79 copies — default copy, manager SoT, the 6 one-off snapshots, and the
  70 other base-A copies.
- ⚠️ `~/.hermes/profiles/worker-plebeian/skills/devops/kanban-worker/SKILL.md`
  — **not** applied. Its frontmatter is already `2.1.0` (the patch's hunk 1 is
  detected as reversed) and it drifts near the anchor. Re-anchor with the
  taxonomy section only and land it as **`2.3.0`**.

**Version numbering — why this patch bumps base A to `2.2.0` and not `2.1.0`.**
`2.1.0` is already taken fleet-wide by the `worker-plebeian` generation, whose
content is **different** (it carries the 2026-09-11 `kanban_request_review`
guidance that this patch does not add). Had this patch also claimed `2.1.0`,
the version field would stop identifying content: two different files would
answer to the same number, and anyone diffing versions would wrongly conclude
that the base-A copies carry the request_review guidance. Reserved numbering:

| version | content |
|---|---|
| `2.0.0` | both older variants (base A default/worker copies, base B manager SoT) |
| `2.1.0` | `worker-plebeian` generation, pre-taxonomy |
| `2.2.0` | base A + taxonomy (**this patch**) and base B + taxonomy |
| `2.3.0` | `worker-plebeian` generation + taxonomy (the re-anchoring follow-up) |

Applied-content fingerprints (post-patch md5, for spot-checking):

| base | applied md5 |
|---|---|
| base A (all 71 copies) | `929c0ccc93b1d22af937ee8476e60eeb` |
| base B (manager SoT) | `90d4914fc677f8d49640ee4a0660528d` |

### Anchor rationale

The patch inserts the section immediately **before `## Heartbeats worth
sending`**, with a one-line leading context (the blank line above that heading)
and a three-line trailing context. Reason: the two dominant variants diverge
*immediately after* the "The block message is what appears…" paragraph (base B
inserts its `review-required:` subsection there), so that paragraph cannot be
used as a leading anchor — the H2 heading is the first line that is identical
**and adjacent** in both. The suite proves the consequence: a zero-fuzz apply to
both variants, and a negative control confirming the hunk does **not** apply to
an unrelated skill doc (no over-broad context).

## Apply procedure (manager)

```bash
cd ~/worktrees/t_87e5657d            # branch hermes-v2/worker-quota-taxonomy

# 0. see what would happen (no writes), for the two canonical live targets
bash patches/apply-kanban-worker_quota-pause-taxonomy.sh --dry-run

# 1. apply to the copy workers actually read (default profile, base A)
bash patches/apply-kanban-worker_quota-pause-taxonomy.sh \
     --target ~/.hermes/skills/devops/kanban-worker/SKILL.md

# 2. apply to the manager SoT (base B) — the git-tracked source of truth
bash patches/apply-kanban-worker_quota-pause-taxonomy.sh \
     --target ~/.hermes/profiles/manager/skills/devops/kanban-worker/SKILL.md

# 3. any base-A worker-profile copy you want in this commit (same patch, same base)
bash patches/apply-kanban-worker_quota-pause-taxonomy.sh \
     --target ~/.hermes/profiles/<profile>/skills/devops/kanban-worker/SKILL.md
```

The helper writes `<file>.bak-<epoch>` before every write, refuses an unknown
base md5 unless `--force` is passed, and restores the backup if the post-apply
marker check fails. Re-running it on an already-patched file is a no-op.

Commits (the helper never commits):

- `~/.hermes/profiles/manager/skills` → commit there (`felixfelix-bot/hermes-manager-skills`).
- The default/worker copies are untracked fleet state; record the applied md5s
  (table above) in the commit message or in this doc, whichever the operator
  prefers.

**Decision the operator/manager still owns:** whether to apply to all 78
compatible copies in one go, or only the default copy + manager SoT and let the
rest drift further. The underlying problem is bigger than this patch — 71
profiles silently shadow the manager SoT with a June snapshot (missing even the
September `review-required`/`kanban_request_review` changes), and one profile
already ran ahead with its own `2.1.0`. The durable fix is the
`hermes-multi-profile-skills` recipe (symlink the shared skills into each
profile so the SoT wins), which is a cross-profile change deserving the same
explicit approval as this patch.

## Verification

```bash
bash tests/test_kanban_worker_quota_taxonomy.sh
# expected (full fleet present): PASS=42 FAIL=0 SKIP=0
#   RED baseline for the fixed properties (new suite vs the pre-fix patch +
#   pre-fix helper): PASS=37 FAIL=5 exit 1 — t12 (version), t19a/t19b
#   (partial-state refusal), t20b/t20c (.rej litter).
#   "skip-" lines appear only for live copies absent on the host; the hermetic
#   fixture legs (t3/t4/t8-t11) always run.
python3 tests/verify_skill_loads.py        # runtime manifest check (exit 3 = SKIP if no scanner)
bash -n ... ; shellcheck -S warning ...    # both scripts clean
```

Post-apply spot check on any target:

```bash
grep -n 'Special prefix: `quota-paused:`' <target>/devops/kanban-worker/SKILL.md
python3 -c "import yaml,sys;t=open(sys.argv[1]).read();print(yaml.safe_load(t.split('---\n',2)[1])['version'])" <target>/devops/kanban-worker/SKILL.md
```

## Rollback

```bash
# a) reverse the patch (verified: reverse dry-run succeeds on an applied file)
cd <skills-root> && patch -p1 -R --batch < <repo>/patches/kanban-worker_quota-pause-taxonomy.patch
# b) or restore the pre-apply backup the helper wrote
cp <file>.bak-<epoch> <file>
```

## Probe recipe (re-derive this doc's tables)

```bash
python3 - <<'PY'
import glob, hashlib, os
for p in glob.glob(os.path.expanduser("~/.hermes/**/kanban-worker/SKILL.md"), recursive=True):
    print(hashlib.md5(open(p,'rb').read()).hexdigest()[:8], p)
PY
# apply probe (per copy, no writes):
W=$(mktemp -d); mkdir -p "$W/skills/devops/kanban-worker"
cp <copy> "$W/skills/devops/kanban-worker/SKILL.md"
(cd "$W/skills" && patch -p1 --batch --fuzz=0 --dry-run < patches/kanban-worker_quota-pause-taxonomy.patch)
```

## Cross-family cold review (Gate 2.5)

| field | value |
|---|---|
| reviewer | `moonshotai/Kimi-K3-TEE` (moonshot family — opposite of the deepseek worker) |
| route | chutes (`llm.chutes.ai`). The zai proxy kimi/glm routes returned HTTP 503 `all providers exhausted (flat router)` at review time; deepseek routes were fine, so this was a **model-route** outage, not a board quota pause (gate file `paused=false`) — the T3.3 taxonomy's own distinction. |
| prompt | diff + task description + Gate-2 test output only; zero design context |
| verdict | **CHANGES_REQUESTED** — 1 major, 4 minor |
| verdict file | `~/.hermes/kanban/boards/hermes-for-friends/attachments/t_87e5657d/kimi-t33-verdict.json` |

All five findings were addressed before re-review:

| # | sev | finding | fix |
|---|---|---|---|
| 1 | **major** | version bump to `2.1.0` collides with the pre-existing `worker-plebeian` `2.1.0`, and the doc sentence "`2.1.0` keeps meaning that copy's content" contradicted the patch's own numbering | patch now bumps to **`2.2.0`**; the doc states the reserved numbering table and the plebeian follow-up moves to `2.3.0` |
| 2 | minor | quota prefixes listed with trailing colons (`ACTIVE 429:`) although `QUOTA_REASON_PREFIXES` is colon-*separated* tokens with no trailing colon | the section now quotes the sweeper's token list verbatim and shows the gate's real emitted reason shapes (`ACTIVE 429: <detail>` etc.) |
| 3 | minor | restore-on-failure left a stray `SKILL.md.rej` in a (possibly live) skills tree | helper removes the `.rej` immediately after the apply; leg **t20** proves no litter |
| 4 | minor | idempotency keyed on one marker — a partial/interrupted apply would be reported "already applied" and skipped | helper requires **all three** markers for the no-op; some-but-not-all is refused as an unknown state; leg **t19** proves the refusal |
| 5 | minor | the insert left 3 extra blank lines (4 consecutive) before the H2 heading | collapsed to a single blank line |

The reviewer independently confirmed: the three contract names are verbatim-correct
against T3.1/T3.2, **no test leg mutates a live skill copy** (the D9=B constraint),
the helper's preflight/backup/restore logic is correct, and the assertions are
non-vacuous (t2e, t10b, t12, t13, t14 specifically called out).

## Follow-ups this patch does not cover

1. Re-anchor for the `worker-plebeian` generation (already `2.1.0`) → land as `2.3.0`.
2. Decide + execute the fleet skill-sync fix (profile-local copies shadow the SoT).
3. The taxonomy is worker-side guidance only: until the dispatcher's board-pause
   lands mid-run, workers still discover the outage by the *signature*, not by
   being told — that is by design (T3.2 pauses stops new claims; this stops
   in-flight grinding).
