#!/usr/bin/env python3
"""D-144: the `delivery` tier and the `delivery_evidence` predicate it requires.

`gates.default.json` routes the tags `delivery-only|evidence-only` to the
`delivery` tier, whose require list is ``["delivery_evidence", "secrets_clean"]``
— no `ci_evidence`, no `tests_green`, no `consolidated`, no cold cross-family
review. Those code gates are STRUCTURALLY unsatisfiable for a card whose whole
deliverable is "post the artifact": there is no code to test, no CI run, nothing
to merge.

That unsatisfiability is not cosmetic. It is what made `gate_tick` block a card
that was already `done`; `done` is dispatchable, so the dispatcher re-ran it,
the worker finished it `done` again, and the next tick blocked it again — every
10 minutes, a full worker session per pass, on a merged deliverable.
`consecutive_failures` stayed 0 the whole time, because every run *succeeded*,
so the failure circuit breaker could never see the loop.

HARDENING (2026-10-06, cold cross-family review of PR #10). The predicate first
shipped as a single regex applied to the AGGREGATE evidence blob — the task
result plus the last 30 comments. Two falsifications came back from the review
and are reproduced in this file:

  1. The blob includes the card's own INSTRUCTIONS. The card that motivated
     D-144 was itself "post the evidence to <PR URL>", so the gate was credited
     by the sentence telling the worker what to do. A pre-completion comment
     naming the target URL is not evidence that anything was posted.
  2. `\\S+\\.(?:mp4|png|webm)` matched a bare `http://a.png` — and any filename
     at all — because it was not anchored to a host. `https://…/invalid/x/blob/y`
     (a host that cannot resolve) credited the `/blob/` branch.

So `delivery_evidence` is now evaluated ONLY against `delivery_evidence_text()`:
the task RESULT plus comments written AFTER `completed_at`. Never the
pre-existing instruction surface.

Prose asserting "delivered" is not evidence either.
"""
import json
import pathlib
import sqlite3
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import gate_engine as ge  # noqa: E402

SPEC = json.loads((REPO / "gates.default.json").read_text())

PR_URL = "https://github.com/felixfelix-bot/hermes-scripts/pull/1"
PR_COMMENT_URL = PR_URL + "#issuecomment-1234567890"
PROSE_ONLY = "Delivered the thing to the operator, all done."
SCAN_CLEAN = "secret-scan: clean (scan_all_repos_for_secrets.sh)"


_DEFAULT = object()


def _evaluate(result, tier="delivery", author="deepseek/deepseek-flash",
              delivery_text=_DEFAULT):
    """`delivery_text` defaults to the result — i.e. "the URL is in the result",
    which is the only shape the hardened predicate credits. Pass it explicitly
    (including ``None``) to exercise a different surface."""
    return ge.evaluate(tier, result, author, gates=SPEC, ci_required=False,
                       secrets_hits=[],
                       delivery_text=result if delivery_text is _DEFAULT
                       else delivery_text)


# ── the predicate itself ────────────────────────────────────────────────────

def test_predicate_exists():
    assert callable(ge.delivery_evidence_present)


def test_published_artifact_urls_are_evidence():
    for url in (
        "https://github.com/felixfelix-bot/hermes-scripts/pull/1",
        "https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890",
        "https://github.com/c03rad0r/foo/pull/12#discussion_r987654321",
        "https://github.com/felixfelix-bot/foo/releases/tag/v1.2.3",
        "https://github.com/felixfelix-bot/foo/blob/master/docs/x.md",
        "https://github.com/felixfelix-bot/foo/commit/abc1234",
        "https://raw.githubusercontent.com/felixfelix-bot/foo/master/x.txt",
        "https://blossom.primal.net/deadbeef.mp4",
        "https://media.c03rad0r.dev/shot.png",
    ):
        assert ge.delivery_evidence_present(url), f"not credited: {url}"


def test_prose_is_not_evidence():
    for prose in (
        PROSE_ONLY,
        "Done. Posted it.",
        "I delivered the video to the PR and everything is complete.",
        "The artifact has been published.",
    ):
        assert not ge.delivery_evidence_present(prose), f"wrongly credited: {prose}"


def test_bare_filename_is_not_evidence():
    """Fix 2: the `\\S+\\.(mp4|png|webm)` branch was unanchored, so a filename
    with no host at all credited the gate (`http://a.png` was the reviewer's
    counterexample)."""
    for junk in ("http://a.png", "http://x.mp4", "shot.png",
                 "see the video at deadbeef.webm", "result: out.png"):
        assert not ge.delivery_evidence_present(junk), f"wrongly credited: {junk}"


def test_placeholder_host_is_not_evidence():
    """A host that cannot resolve is a documented SHAPE, not a published
    artifact — the same rule RE_CONSOLIDATED already applies to merges."""
    for junk in (
        "https://not-a-real-host.invalid/x/blob/y",
        "https://example.org/shot.png",
        "https://blossom.example.com/deadbeef.mp4",
        "https://github.com.example.com/o/r/pull/3",
        "https://localhost/pull/3",
        "https://github.com/o/r/pull/%s",
        "https://github.com/o/r/pull/<N>",
    ):
        assert not ge.delivery_evidence_present(junk), f"wrongly credited: {junk}"


def test_a_failure_report_is_not_evidence():
    """The card that motivated this tier was "post the evidence to <PR URL>";
    a card that FAILED to post quotes the same URL. Only the scoping fix
    (result / post-completion comments) can tell them apart."""
    assert not ge.delivery_evidence_present(
        f"Blocked: could not post to {PR_URL} — the token lacks write access.")


# ── the predicate wired into the tier (not just defined) ────────────────────

def test_delivery_card_with_a_published_url_passes():
    r = _evaluate(f"{PR_COMMENT_URL}\n{SCAN_CLEAN}")
    assert "delivery_evidence" in r["passed"], r
    assert r["verdict"] == "pass", r


def test_delivery_card_with_prose_only_blocks():
    r = _evaluate(f"{PROSE_ONLY}\n{SCAN_CLEAN}")
    assert "delivery_evidence" in r["missing"], r
    assert r["verdict"] == "block", r


def test_url_outside_the_result_surface_cannot_credit():
    """Fix 1, at the evaluate() boundary: the aggregate blob carries the URL but
    the scoped surface does not -> nothing is credited. A caller that has not
    computed the scoped surface gets a BLOCK, never a pass (fail closed)."""
    aggregate = f"Instructions: post the evidence to {PR_COMMENT_URL}\n{SCAN_CLEAN}"
    for scoped in ("", PROSE_ONLY, None):
        r = _evaluate(aggregate, delivery_text=scoped)
        assert "delivery_evidence" in r["missing"], (scoped, r)
        assert r["verdict"] == "block", (scoped, r)
    r = _evaluate(aggregate, delivery_text=f"Posted it: {PR_COMMENT_URL}")
    assert r["verdict"] == "pass", r


# ── the scoping helper against a real board DB ──────────────────────────────

def _board(tmp_path, result, pre=(), post=(), completed_at=None,
           body="tags: delivery-only"):
    """A minimal board DB: one `done` card with pre- and post-completion
    comments. The tag lives in the body, the instructions in the comments — the
    shape of the card D-144 exists for. Returns (board_name, task_id)."""
    board = "delivery-e2e"
    d = tmp_path / board
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / "kanban.db")
    c.execute("create table tasks (id text primary key, title text, body text,"
              " assignee text, status text, priority integer, result text,"
              " completed_at integer, created_at integer)")
    c.execute("create table task_comments (id integer primary key autoincrement,"
              " task_id text, author text, body text, created_at integer)")
    now = int(time.time())
    done = completed_at if completed_at is not None else now - 60
    c.execute("insert into tasks values (?,?,?,?,?,?,?,?,?)",
              ("t_deliv", "Deliver the thing", body, "worker-x", "done", 0,
               result, done, done - 3600))
    rows = [("t_deliv", "manager", b, done - 600 - i) for i, b in enumerate(pre)]
    rows += [("t_deliv", "worker-x", b, done + 30 + i) for i, b in enumerate(post)]
    c.executemany("insert into task_comments (task_id, author, body, created_at)"
                  " values (?,?,?,?)", rows)
    c.commit()
    c.close()
    return board, "t_deliv"


def _evaluate_task(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(ge, "BOARDS", tmp_path)
    monkeypatch.delenv("GATES_ENFORCE_SINCE_TS", raising=False)
    board, tid = _board(tmp_path, **kw)
    return ge.evaluate_task(board, tid, SPEC)


def test_pre_completion_comment_does_not_credit(tmp_path, monkeypatch):
    """The D-144 card shape: the target URL is in the card's own instructions
    and in a comment written before completion. Nothing was posted."""
    r = _evaluate_task(
        tmp_path, monkeypatch,
        result=f"Done — followed the instructions.\n{SCAN_CLEAN}",
        pre=[f"TASK: post the evidence to {PR_COMMENT_URL} and reply here."])
    assert "delivery_evidence" in r["missing"], r
    assert r["verdict"] == "block", r


def test_url_in_the_result_credits(tmp_path, monkeypatch):
    r = _evaluate_task(
        tmp_path, monkeypatch,
        result=f"Posted the evidence: {PR_COMMENT_URL}\n{SCAN_CLEAN}",
        pre=[f"TASK: post the evidence to {PR_COMMENT_URL} and reply here."])
    assert "delivery_evidence" in r["passed"], r
    assert r["verdict"] == "pass", r


def test_comment_written_after_completion_credits(tmp_path, monkeypatch):
    """A card that completes and then lands its evidence in a follow-up comment
    (the normal kanban shape after `complete`) still credits."""
    r = _evaluate_task(
        tmp_path, monkeypatch,
        result=f"Completed.\n{SCAN_CLEAN}",
        pre=[f"TASK: post the evidence to {PR_COMMENT_URL}"],
        post=[f"Evidence published: {PR_COMMENT_URL}"])
    assert "delivery_evidence" in r["passed"], r
    assert r["verdict"] == "pass", r


# ── the spec routes the tags (D-165: a typo can never fail open) ─────────────

def test_spec_defines_the_delivery_tier():
    tier = SPEC["tiers"].get("delivery")
    assert tier, "no `delivery` tier in gates.default.json"
    assert tier["require"] == ["delivery_evidence", "secrets_clean"], tier
    assert tier["enforce"] == "block", tier
    for absent in ("ci_evidence", "tests_green", "consolidated"):
        assert absent not in tier["require"], f"{absent} is unsatisfiable here"


def test_spec_routes_the_explicit_delivery_tags():
    routes = SPEC["tag_tiers"]
    for tag in ("delivery-only", "evidence-only"):
        assert routes.get(tag) == "delivery", f"{tag} -> {routes.get(tag)}"


def test_generic_delivery_words_do_not_route():
    """Fix 3: `delivery` / `evidence` are ordinary English words that appear in
    unrelated card text ("evidence: the test passed"). Routing them let one
    line of body text skip the code tier's tests/CI/consolidated gates, so only
    the explicit `-only` forms route. An untiered card falls back to `code`
    (D-165 fail-safe), which is the correct default for work of unknown type."""
    routes = SPEC["tag_tiers"]
    for tag in ("delivery", "evidence"):
        assert tag not in routes, f"{tag} still routes to {routes.get(tag)}"
    known = ge.known_tags(SPEC)
    assert {"delivery-only", "evidence-only"} <= known, sorted(known)
    assert not ({"delivery", "evidence"} & known), sorted(known)
    assert ge.classify_tier("any-board", ["delivery"], SPEC) == "code"
    assert ge.classify_tier("any-board", ["evidence"], SPEC) == "code"
    assert ge.classify_tier("any-board", ["delivery-only"], SPEC) == "delivery"
    assert ge.classify_tier("any-board", ["evidence-only"], SPEC) == "delivery"


# ── the bug D-144 exists to kill: the code tier on a delivery-only card ─────

def test_code_tier_on_a_delivery_card_is_unsatisfiable():
    """Guards the premise: with no `delivery` tier, a delivery-only card is
    asked for code evidence it cannot produce. If this ever passes cheaply, the
    delivery tier has become decorative."""
    r = _evaluate(f"{PR_COMMENT_URL}\n{SCAN_CLEAN}", tier="code")
    assert r["verdict"] == "block", r
    assert set(r["missing"]) & {"ci_evidence", "tests_green", "consolidated"}, r
