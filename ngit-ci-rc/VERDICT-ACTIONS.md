# Consultant verdict: APPROVE-WITH-CHANGES (deepseek/deepseek-v4-flash, 58 s, 16.3k tokens)

Full text: `verdict-deepseek_deepseek-v4-flash.md`. These are the changes the plan must absorb
before the RC. Operator decided B1 = use hermes VPS (23.182.128.219); the review says the *seam*
is premature — measurement first, then choose placement.

## Must change before the RC

1. **Do not pick a seam before the byte measurement** (reviewer §1, §2). The split buys RAM only:
   hermes is 2 vCPU, same as DQ05, and the UPX legs are CPU-bound (~29 min/leg on 2 CPUs).
   Preferred shape if we move: **one coordinator on hermes, DQ05 out of ngit CI entirely** —
   not a repo-level split. A split costs a second control plane, a second secret location, a
   migration window, and the duplicate-announcement class of bug.
2. **Ownership must be mechanically exclusive** before any watch-list edit: no lock, no dedupe,
   no startup assertion exists today, and both hosts would hold MODULE-REPO during the window.
3. **Never restart the coordinator while runs are in flight** — the watch-list edit restarts it
   and drops/orphans running jobs (DQ05 had 5 workflows running at 66c1f707).
4. **C.2 first:** port `verify-publication` (only under `.github/` today → runs nowhere) into
   `.ngit`, and make it a **pre-announcement precondition**, not a post-hoc audit.
5. **Completeness gate + idempotency:** move all kind-1063 emission out of per-leg shards into a
   single terminal announce step; check a checked-in expected matrix (14 ipk + 3 apk) against the
   kind-30078 manifest; key the 30078 `d`-tag on version+arch+commit and refuse to re-announce a
   published tuple. Precedent: the 09-12 run timed out *after* announcing 5 ipks.
6. **Signing-key home is a hard blocker** — decide it before cutting an RC; every kind-9840 in the
   chain must be maintainer-signed or the shard publishes nothing, silently.
7. **Shard by cost class, not arch:** S2a = all `compression: none` legs (~7 min); S2b…S2n = one
   UPX leg each (60 s of headroom against 1800 s — re-measure on the target host); S2x = one
   `.apk` target per invocation, proven end-to-end.
8. **Data accounting:** measure on the target host across a whole shard chain, split pull vs
   upload bytes, cold vs warm cache, and check **where the git mirror lives** — if the mirror is
   on DQ05 and the builder on hermes, the split *adds* traffic to the constrained link.
9. **#418 is docs-only** — it does not change any consumer's `nak req -a 5075e61f…` filter. State
   that RC consumers arrive via the **feed package**, and that the Nostr channel is new-key-only
   until consumers migrate.
10. **Missing guards to add:** no rollback/abort semantics for a timed-out shard; no alerting on a
    stalled 30078/9840 chain, a silently-ignored non-maintainer trigger, or a stale bridge head
    marker; no fast-forward-only check / branch protection on the plain-push bridge (the root
    cause that just cost a day); no disk/RAM preflight before a run.

## Order the reviewer prescribes

A (done) → **measurement** → **B decision** (placement *and* secret home) → **C.2 + C.1** → **D**
→ **F (RC)** → **C.3** → **E (remainder)**.

## Drop / defer

- Stage 2 on HOST-B (busy, 7 G free, 7 orphan `act-*` containers — reclaim, don't add load).
- Widening the bridge branch list for 15 PRs (global blast radius for a scoped problem); use
  `pr/<slug>` / `ci/<slug>` instead.
- Designing UPX shard boundaries before re-measuring 29 min/leg on the target host.

## Top 3 risks

1. Partial release announced as complete (the only completeness gate runs nowhere).
2. Duplicate ownership → duplicate kind-1063 announcements.
3. Release key home undecided + unsigned chain stalls invisibly.
