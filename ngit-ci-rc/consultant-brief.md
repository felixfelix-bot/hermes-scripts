# Consultant brief — adversarial review of the ngit-CI plan (tollgate RC)

You are reviewing a PLAN, not code. Be adversarial: find the decisions that are wrong, the constraints
the author has not accounted for, and the steps that will fail in practice. You have no tools — every
fact must come from the material; label anything you cannot verify as UNVERIFIED.

Context you are given: a fleet whose GitHub Actions is dead org-wide, a self-hosted Nostr CI
(`ngit-ci`, a Rust coordinator running GitHub-Actions-format workflows through `act`), one
data-limited host (DQ05) that currently runs the only coordinator, two VPS candidates, an imminent
alpha release candidate installed from an OpenWrt feed, and 15 open PRs that need CI evidence.

Answer these questions explicitly, numbered:

1. **Coordinator placement.** Is the proposed repo-level split (heavy OpenWrt repos on the idle
   `hermes` VPS, light lanes on DQ05) the right seam? Name the failure modes it introduces (duplicate
   runs, duplicate signed release announcements, watch-list drift, secret sprawl, operator load) and
   propose the seam you would choose instead if you disagree.
2. **Data budget.** The operator flagged DQ05's limited data. What does one full release-pipeline run
   actually consume (images, toolchain, module graph, artifact uploads), and what is the minimum
   measurement that must be taken before committing either host to bulk builds?
3. **Sharding.** Stage 2 of the release pipeline does a 14 `.ipk` + 3 `.apk` matrix and cannot fit the
   1800 s per-invocation ceiling (5 `compression: none` legs ≈ 7 min total; `upx --ultra-brute` ≈
   29 min per leg on 2 CPUs; the SDK/`.apk` legs got no slot). The handover between the two workflow
   files is a kind-30078 record plus a kind-9840 manual trigger (there is no `needs:` across files and
   `actions/upload-artifact` is unavailable). Design the shard boundaries you would use, name the
   race/idempotency hazards in that handover, and say how a partial run must be prevented from
   announcing an incomplete release.
4. **Release integrity.** Before an RC can be published from this lane, what must be true about the
   signing key, the publication-verification gate, and the consumer's view (a consumer filtering the
   dead historical publisher key currently sees nothing)? What is the single most likely way an RC
   gets published wrong here?
5. **Scheduling / order of operations.** Given priority SOON (days, not minutes) and the RC cut
   pending: order the workstreams A–F and say which items must land before the RC, which must land
   after, and which should be dropped or deferred entirely. Flag anything in the plan that is
   premature or busy-work.
6. **What is missing.** Name the checks, invariants or rollback steps the plan does not mention.

Output contract: start with `REVIEWER: <model>`; then numbered answers 1-6; then a short
`TOP 3 RISKS` list; then a final line exactly of the form `VERDICT: <APPROVE|APPROVE-WITH-CHANGES|REJECT> — <one sentence>`.
Be concrete and terse. Prefer a few verified points over many speculative ones.
