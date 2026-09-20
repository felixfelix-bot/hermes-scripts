REVIEWER: deepseek/deepseek-v4-flash
provider_seen: deepseek-flash
elapsed_s: 58
usage: {"prompt_tokens": 4340, "completion_tokens": 11913, "total_tokens": 16253, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 8478}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 4340}

REVIEWER: deepseek/deepseek-v4-flash

## 1. Coordinator placement — the seam is wrong

The proposal (§B) is a **repo-level** split: heavy OpenWrt repos (MODULE-REPO, packages, feed) → `hermes`; "light lanes" stay on HOST-C.

**Failure modes it introduces:**

- **Migration window with double ownership.** HOST-C's `.env` currently holds `NGIT_CI_REPOS=<22 explicit npub/<repo-id> entries incl. MODULE-REPO, packages, feed>`. Until that list is edited *and the coordinator restarted*, both hosts watch MODULE-REPO → the exact outcome the plan names: "duplicate kind-1063 release announcements (a fake duplicate release)". No lockdown, no dedupe, no startup assertion that repo ownership is exclusive is specified anywhere in the material.
- **The restart is not free.** `ngit ci status 66c1f707` shows 5 workflows `running` right now and the coordinator is `Up 3 days`. Editing the watch list drops or orphans in-flight runs — including the very run that proves the ported pipeline.
- **Unnamed repo.** The seam names MODULE-REPO/packages/feed, but the release workflows are `build-package{,-binaries}.yml` at `main 66c1f707` and the bridge target is called `UPSTREAM-REPO`. Whether the repo carrying the release workflows is the repo being moved is **UNVERIFIED** from this material. If it isn't, the split moves nothing that matters and adds a control plane for free.
- **Watch-list / secret / operator drift.** Two `NGIT_CI_REPOS`, two `.env`s, two upgrade paths. `NGIT_CI_SECRET_TMBG__NSEC_HEX` is "currently provisioned operator-side on HOST-C"; the split forces a choice between copying the maintainer nsec to a second host (sprawl) or having the release lane unreachable from the host that owns the heavy repos (the plan's own open question 2).
- **Bootstrapping loop.** CI-infra tasks are boarded on `MODULE-REPO`; if MODULE-REPO's CI now lives on `hermes`, every change to hermes' own coordinator config must be verified through the coordinator under change.
- **It doesn't fix the stated problem.** `hermes` is **2 vCPU** — the same as HOST-C. The UPX legs are CPU-bound ("≈29 min per leg on 2 CPUs"). Moving gains RAM headroom only (HOST-C: 3 G avail → hermes: 15 G). The `--cpus=2 --memory=4g` act opts are a HOST-C setting; nothing says they change.

**Seam I would choose:** one coordinator, on `hermes`, and take HOST-C out of ngit CI entirely (keep it for the non-CI workloads it already runs: `llama-server`, `fips-exit-*`). Rationale: the split buys only "the light repos don't move" — a convenience — while paying for a second control plane, a second secret location, a migration window, and the duplicate-announcement class of bug. The one argument for splitting (hermes' data allowance unknown) is a measurement, not an architecture. **Do not pick any seam before the measurement in (2).**

**On HOST-B:** drop it. 7 G RAM, 23 G free, "busy (relays/mint/buzz/3 hermes agents)", 7 orphan `act-*` containers. Reclaim the orphans; don't add stage 2 there. Note the plan's "~8.7 GB" reclaim figure does **not** appear in the host table (which says 29 GB images, 4.8 GB reclaimable) — **UNVERIFIED**, measure before citing.

## 2. Data budget — nothing here is measurable yet

Every byte figure is absent. What the material gives is *time*: 5 `compression: none` legs ≈ 7 min total; UPX ≈ 29 min/leg; stage 1 `success 706.7 s`; stage 2 `timed_out at the 1800 s ceiling`. Image sizes, Go toolchain, module graph, and the 14 `.ipk` + 3 `.apk` payloads are all **UNVERIFIED**. `NGIT_CI_ACT_CACHE_MAX_BYTES=10737418240` is a cache *cap*, not traffic — and a cache-miss run can move up to 10 GiB of it.

Also note you **cannot measure "one full pipeline run" in one invocation** — it exceeds 1800 s by construction. The unit of measurement is the whole shard chain.

**Minimum measurement before committing either host to bulk builds:**
1. NIC-level byte counters on the *target* host across a full shard chain (not one `act` invocation).
2. Break out **pull** bytes separately: per-SDK-image `docker pull` (`openwrt/sdk:<target>@sha256:…`), Go toolchain, GOPROXY module graph. If pulls dominate, a warm cache makes steady-state runs cheap and the split is pointless.
3. Break out **upload** bytes to Blossom per `.ipk`/`.apk`, per mirror — the kind-1063 `x`-tag carries sha256, not size.
4. Run it **twice**: cache-cold (worst case) and cache-warm (steady state).
5. Unaccounted term to check: where the git mirror lives. If the mirror is on HOST-C and the builder is on `hermes`, every run re-fetches over the limited link — the split would *add* traffic to the constrained host. **UNVERIFIED.**

## 3. Sharding stage 2

**Boundaries.** The cost distribution is pathological: 5 `compression: none` legs ≈ 7 min *combined*, one UPX leg ≈ 29 min. Shard by cost class, not by arch:
- **S2a** — all `compression: none` legs in one invocation (~7 min). Trivially fits.
- **S2b…S2n** — exactly one UPX leg per invocation. 29 min ≈ 1740 s against a 1800 s ceiling is **60 s of headroom**; any pull or build on top of the 29 min blows it. Whether that 29 min already includes the build is **UNVERIFIED** — re-measure on the target host before freezing this boundary. If it doesn't fit, the fix is CPUs, not more shards.
- **S2x** — one SDK/`.apk` target per invocation (largest unknown byte cost); prove one end-to-end as planned.

Flag the framing: this is presented as a sharding problem, but 29 min/leg at 2 vCPU and `NGIT_CI_MAX_CONCURRENT_JOBS=1` means sharding only *bounds* the run — it does not make it fast. Serial UPX legs plus a manual kind-9840 between each is measured in hours of operator presence.

**Handover hazards (kind-30078 state + kind-9840 manual trigger):**
- **Silent stall.** A trigger "signed by a non-maintainer is published but silently ignored", and results are advisory kind-9841/9842 — no page. A missed signature is indistinguishable from "still running".
- **Non-idempotent re-execution.** Relay re-delivery or an operator retrying an unseen shard re-runs it and re-emits kind-1063 for the same (version, arch, sha256). No idempotency key is specified. Key the 30078 `d`-tag on version+arch+commit and refuse to re-announce a published tuple.
- **Ref/commit race.** "The ref must exist on the mirror BEFORE the trigger" and the bridge is plain-push/no-force; this incident was exactly a rewritten default branch. Every shard must pin the commit SHA in the 30078 and assert the mirror head still contains it before publishing.
- **Lost update** on the shared 30078 if concurrency is ever raised above 1 (the plan floats a bigger host; HOST-C's `=1` is what currently hides this).
- **Half-`needs:` emulation.** With no `needs:` across files, "stage 2 ran" does not mean "stage 1's artifacts exist". Each shard needs an explicit precondition check, not an assumption.

**Preventing an incomplete announcement.** The precedent is fatal: stage 2 `timed_out at the 1800 s ceiling **after** publishing 5 signed kind-1063 ipks`. Announcements are emitted per-leg, inside the shard, before completeness is known. Fix: **(a)** move all kind-1063 emission out of per-leg shards into a single terminal announce step; **(b)** check a checked-in expected matrix (14 ipk + 3 apk, or documented exclusions) against the 30078 manifest and refuse to run otherwise; **(c)** make the C.2 `verify-publication` gate a *pre*-announcement precondition, not a post-hoc audit.

## 4. Release integrity

- **Signing key:** its home is an open question in the plan (open q2) and is therefore a hard blocker — you cannot cut an RC before deciding it. Compounding: "secrets only reach maintainer-triggered runs", so *every* 9840 in the chain must be maintainer-signed or the shard publishes nothing, silently.
- **Publication-verification gate:** `#406`'s `verify-publication` "exists only under `.github/workflows/` today, i.e. it runs nowhere". The single gate that would catch a bad publication is not executed. C.2 must land before the RC. As specified it is also necessary-but-insufficient: it checks that published things have events, not that no expected arch is *missing* — hence the matrix in (3).
- **Consumer view:** `#418` is **docs-only**. Documenting the new publisher key does not change any existing consumer's `nak req -a 5075e61f…` filter. State explicitly that RC consumers arrive via the **feed package** (workstream F), and that the Nostr channel is new-key-only until consumers migrate. Whether the new key is cryptographically delegated from `5075e61f…` or unrelated is **UNVERIFIED** — if unrelated, there is no verifiable rotation path.

**Single most likely way the RC gets published wrong:** the announce step runs with no completeness gate (the only one, `#406`, runs nowhere), and the 09-12 precedent shows a run can time out *after* announcing — so a partial RC (missing arch or format, single-mirror artifact) is announced as complete while CI shows nothing red. Runner-up worth noting: the release version is computed by a stage-1 job (`determine-versioning`) while the feed `master` still pins `0.6.0-alpha1` and the installed tag is `v0.6.0-alpha2-pre7` — three places for the version string to disagree, and the gate checks "the published version+channel".

## 5. Scheduling / order of operations

**Order:** A (done) → **measurement (2)** → **B decision** (single coordinator + secret home) → **C.2 + C.1** → **D** → **F** (RC) → **C.3** → **E (remainder)**.

**Before the RC:** the byte measurement; the B decision (placement *and* secret location); **C.2** (port `verify-publication` into `.ngit`); **C.1 / #418** (docs both keys, cheap, blocks consumer discoverability); **D** (a stage 2 that can actually finish, plus one repository-verified `.apk`); **E only for the PRs feeding the RC**.

**After the RC:** **E** for the remaining PRs; **C.3** docs (`docs/release-process.md` / `.ngit/README.md`); orphan reclamation on HOST-B.

**Drop / defer:** running stage 2 on **HOST-B** (busy, 7 G, orphan `act-*`); **widening the bridge branch list** (E option 2) — a global config change to serve 15 PRs widens the blast radius of the exact failure that just cost a day, for a scoped problem; use `pr/<slug>` + `ci/<slug>` instead.

**Premature / busy-work:** choosing the repo-level seam before the measurement (Q1 is currently unanswerable); designing UPX shard boundaries before re-measuring 29 min/leg on the target host; framing `#418` as *fixing* the consumer view when it only documents a key.

## 6. What is missing

- No mechanical enforcement that repo→coordinator ownership is exclusive (no lock, no dedupe, no startup assertion).
- No idempotency key on publication; no **retraction path** for a bad or duplicate kind-1063 (NIP-09 or tombstone) despite naming that exact risk.
- No checked-in expected release matrix.
- No alerting on: a stalled 30078/9840 chain, a silently-ignored non-maintainer trigger, or a stale **bridge head marker** (held at `040dd7fa` — nothing says how long the outage ran before a human noticed).
- No durable guard against the repeat of this incident: the bridge is "plain push, no force" and "a rewritten default branch breaks the mirror and silently halts CI for every later commit". A fast-forward-only check or mirror-branch protection is absent.
- No rollback/abort semantics: a shard that times out must not emit an announcement, but the evidence shows it did.
- No disk/RAM preflight gate before a run (HOST-C 103 G free at 77 % used; hermes 74 G; HOST-B 23 G vs 29 GB of images).
- No decision on `--cpus/--memory` or concurrency on the new host — the HOST-C values are being carried implicitly.
- No independent verification: the 09-12 "consumer check" was the operator checking their own publication.
- No refresh schedule for the `pr/<slug>` / `ci/<slug>` refs pushed in E (stale refs accumulate).
- No statement of who signs 9840s during unattended windows.
- UNVERIFIED: what the "one container in a crash loop" on HOST-C is and whether it is part of the CI stack.

## TOP 3 RISKS

1. **Partial release announced as complete** — the 09-12 run timed out *after* publishing 5 kind-1063 ipks, and the only completeness gate (`#406`) runs nowhere.
2. **Duplicate ownership / duplicate announcements** — the repo-level split creates a window where MODULE-REPO is in both `NGIT_CI_REPOS` lists, with no mechanical guard against the plan's own stated outcome, "a fake duplicate release".
3. **Release key and unsigned-chain stall** — the secret's home is still an open question, and a non-maintainer-signed 9840 is "published but silently ignored", so the chain can stall invisibly with advisory results and no alert.

VERDICT: APPROVE-WITH-CHANGES — the lane is viable and A is proven, but the coordinator seam is premature without a byte measurement, C.2 and the signing-key home must land before any RC, and the shard design needs a pre-announcement completeness gate plus idempotency so a timeout can never announce an incomplete release.