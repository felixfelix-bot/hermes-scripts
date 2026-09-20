# PLAN — ngit CI as the working CI/release lane (pre-RC)

Operator: c08r4d0r (Felix), 2026-09-20. Priority: **SOON** (scheduled, not all dispatched NOW).
Approved already: (A) force-push the ngit mirror — DONE; (B) bring the coordinator back / offload heavy
builds off the data-limited DQ05 line; (C) land the docs/verification follow-ups; (D) shard the release
pipeline and prove the `.apk` leg; (E) PR-level ngit CI evidence for the 15 open PRs.

## Why this exists

GitHub Actions is dead **org-wide** on `OpenTollGate/tollgate-module-basic-go`: every run, `main`
included, has sat in `queued` since 2026-08-27 (billing/runner-group level, not code). No artifact has
been published in any channel since then. ngit CI (`ngit-ci`) is therefore the only lane that can build,
test and publish. The alpha RC for the review club is installed from the **FreedomTechFeed feed**
(latest tag `v0.6.0-alpha2-pre7`), which pins a module commit — so the RC needs (i) a publishable ngit
pipeline and (ii) evidence on the PRs that feed it.

## Verified state (2026-09-20, 12:2x–12:5x UTC)

| Fact | Evidence |
|---|---|
| Mirror was diverged → bridge failing every 5 min | `bridge.log` 12:04/12:08Z: `TRIGGER mirror_push FAILED … mirror head=65c3c4ea != 351c3354 … tip of your current branch is behind`; head marker held at `040dd7fa` |
| Divergence is lineage-only, nothing lost | merge base `7e999bdd`; 221 mirror-only / 15 GitHub-only commits; **0 files exist only on the mirror** |
| **A DONE — mirror reconciled** | force-pushed GitHub main `66c1f707` → mirror; `git ls-remote` both sides equal; old tip kept as `archive/mirror-2026-09-15` (`65c3c4ea`) |
| ngit CI now firing at main | `ngit ci status 66c1f707`: 5 workflows `running` — build-package-binaries, build-package, go-test, repro-check, test; `Covered by a maintainer request`; `integrity: commit present, workflow hash matches` |
| Release pipeline already ported | PR **#410 MERGED** today (merge commit = main tip `66c1f707`); `.ngit/act/workflows/build-package{,-binaries}.yml` on main |
| Pipeline proven end-to-end (09-12, `b25d8a28`) | stage 1 `success` 706.7 s (3 jobs); stage 2 `timed_out` at the 1800 s ceiling **after** publishing 5 signed kind-1063 ipks; one blob fetched from 2 Blossom mirrors with matching sha256 |
| Coordinator is UP on DQ05 | `ngit-ci-deploy-coordinator-1` Up 3 days, `dind-1` Up 6 days; `NGIT_CI_MAX_CONCURRENT_JOBS=1`; act opts `--cpus=2 --memory=4g`; job timeout default 1800 s |
| DQ05 host | netbird `100.90.22.201` (alias `remote-worker`); 2 vCPU, 10 G RAM (3 G avail), 103 G free, 77 % disk; operator says **data-limited** |
| VPS candidates | `hermes` 23.182.128.219 — 2 vCPU, 15 G RAM, 74 G free, docker 29.8.1, load 0.04, idle. `testserver2` 23.182.128.51 — 2 vCPU, 7 G RAM, 23 G free, busy (relays, mint, buzz, 3 hermes agents), 29 GB docker images, orphan `act-*` containers Up 2 days |
| Guard rails | ngit CI cannot satisfy a GitHub required status check (signed Nostr events only) → **advisory**; merge stays manual. `schedule:` unsupported; `container:`/`services:` with options refused; no `GITHUB_TOKEN`; secrets only on maintainer-triggered runs |
| Workflow placement rule | ngit-ci reads `.ngit/act/workflows/` **only**; the GH→ngit bridge mirrors `main`/`master` **only**, so a GitHub PR branch never triggers ngit CI |

## Workstreams

### B — coordinator placement vs the data-limited DQ05 line
Constraint: the coordinator host both downloads (Go toolchain, `openwrt/sdk` images, module graph) and
uploads (14 `.ipk` + 3 `.apk` to Blossom). DQ05 is the data-limited link, and 2 CPU also makes the UPX
legs slow.
Proposal: move the **heavy** repos (tollgate-module-basic-go, packages, feed) to a coordinator on the
**`hermes` VPS** (idle, 15 G RAM, 74 G free) with the dind sidecar; keep the light lanes on DQ05 for
its other repos. **Repo-level ownership only** — two coordinators watching the same repo would double
every run and publish duplicate kind-1063 release announcements.
Open questions for the consultant lane: (1) is repo-level split the right seam, or should we move the
whole coordinator and leave DQ05 out of ngit CI entirely? (2) where do we put the release-signing
secret `NGIT_CI_SECRET_TMBG__NSEC_HEX` (currently provisioned operator-side on DQ05) — provision it on
the new host, or trigger release lanes only from the host that has it? (3) is `hermes` big enough
(2 vCPU / 15 G) for stage 1 + stage 2, or should stage 2 run on `testserver2`? (4) how do we measure
and cap the data consumed by one full stage-2 run before committing DQ05's link?
Also: identify and reclaim the orphan `act-*` containers on `testserver2` (~8.7 GB) and confirm no
abandoned coordinator there.

### C — land the release-lane follow-ups
1. **#418** (`docs(agents): document both release publisher keys`, docs-only, open) — must land before
   the RC: consumers filtering `nak req -a 5075e61f…` (the dead GitHub-Actions key) currently see none
   of the new announcements.
2. **Port #406's `verify-publication` job into the `.ngit` twin.** It exists only under
   `.github/workflows/` today, i.e. it runs nowhere. The ngit pipeline is what actually publishes, so
   the publication gate must live there: every (arch, format) published must have a kind-1063 event
   for the published version+channel, and artifacts must be servable from ≥2 mirrors with the `x`-tag
   sha256.
3. Add the ngit release path to `docs/release-process.md` / `.ngit/README.md` so the next release does
   not depend on GitHub Actions.

### D — shard stage 2 and prove the `.apk` leg
Facts: one `act` invocation is bounded by 1800 s; the 5 `compression: none` legs ≈ 7 min total;
`upx --ultra-brute` ≈ 29 min per leg at 2 CPU; the `.apk`/SDK legs did not get a slot.
Plan: split stage 2 into per-arch (or per-format) invocations, each ≤ ~1500 s, chained through the
existing kind-30078 handover (no `needs:` across files, `actions/upload-artifact` is unavailable);
run SDK packaging via `docker run openwrt/sdk:<target>@sha256:…` against the dind sidecar; prove at
least one `.apk` leg end-to-end with a consumer-side sha256 check against its kind-1063 event.
Exit criterion: every arch/format in the release matrix has a green result **or** a documented
exclusion, and one `.apk` is repository-verified.

### E — PR-level ngit CI evidence for the 15 open PRs
The bridge mirrors `main`/`master` only, so no PR branch triggers ngit CI. Two options:
push each PR head to the mirror as `pr/<slug>` (PR semantics on ngit) **plus** a plain `ci/<slug>` ref
(the eligibility check needs a branch ref), then trigger the fast lanes per PR with the maintainer key;
or widen the bridge branch list (global config → scope deliberately).
Exit criterion: for each PR under consideration for the RC, a 9842 `success` at the PR head SHA,
signed by our coordinator, quoted in the PR thread.

### F — RC assembly (after C/D)
Repin the feed to the RC module commit (feed `master` still pins `0.6.0-alpha1`), tag, release, then
verify the tester install path end-to-end (feed package, not Nostr dev artifacts).

## Scheduling (SOON)
Board: `tollgate-module-basic-go` (CI infra tasks may go on `ngit-ci`). Every task carries an
evidence field (command + result) and, where it lands code, the review + `ci_evidence` gates.

## Risks
- Duplicate coordinators on one repo ⇒ duplicate kind-1063 announcements (a fake duplicate release).
- Secrets: the release lane needs the CI nsec on whichever host runs it; never commit it, never echo it.
- VPS data allowance unknown for `hermes`/`testserver2` — measure one stage-2 run before bulk runs.
- ngit results are advisory: nothing here unblocks a GitHub *required* check; the merge decision stays
  the operator's.

## Consultant verdict (2026-09-20)

APPROVE-WITH-CHANGES — see consult/VERDICT-ACTIONS.md. Measurement before placement decision;
C.2 (verify-publication port) and the signing-key home are hard blockers for the RC; kind-1063
emission moves to a single terminal announce step with a completeness matrix + idempotency.
