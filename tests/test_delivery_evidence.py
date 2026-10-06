#!/usr/bin/env python3
"""D-144: the `delivery` tier and the `delivery_evidence` predicate it requires.

`gates.default.json` routes the tags
`delivery-only|delivery|evidence-only|evidence` to the `delivery` tier, whose
require list is ``["delivery_evidence", "secrets_clean"]`` — no `ci_evidence`,
no `tests_green`, no `consolidated`, no cold cross-family review. Those code
gates are STRUCTURALLY unsatisfiable for a card whose whole deliverable is
"post the artifact": there is no code to test, no CI run, nothing to merge.

That unsatisfiability is not cosmetic. It is what made `gate_tick` block a card
that was already `done`; `done` is dispatchable, so the dispatcher re-ran it,
the worker finished it `done` again, and the next tick blocked it again — every
10 minutes, a full worker session per pass, on a merged deliverable.
`consecutive_failures` stayed 0 the whole time, because every run *succeeded*,
so the failure circuit breaker could never see the loop.

`delivery_evidence` is the delivery-tier counterpart of `ci_evidence`. A code
card proves it passes CI; a delivery-only card must prove the artifact PUBLISHED
somewhere a human can open. Prose asserting "delivered" is not evidence.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import gate_engine as ge  # noqa: E402

SPEC = json.loads((REPO / "gates.default.json").read_text())

PR_COMMENT_URL = (
    "Posted the evidence as a PR comment "
    "https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890"
)
PROSE_ONLY = "Delivered the thing to the operator, all done."
SCAN_CLEAN = "secret-scan: clean (scan_all_repos_for_secrets.sh)"


def _evaluate(text, tier="delivery", author="deepseek/deepseek-flash"):
    return ge.evaluate(tier, text, author, gates=SPEC, ci_required=False,
                       secrets_hits=[])


# ── the predicate itself ────────────────────────────────────────────────────

def test_predicate_exists():
    assert hasattr(ge, "RE_DELIVERED_URL")


def test_published_artifact_urls_are_evidence():
    for url in (
        "https://github.com/felixfelix-bot/hermes-scripts/pull/1",
        "https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890",
        "https://github.com/c03rad0r/foo/pull/12#discussion_r987654321",
        "https://github.com/felixfelix-bot/foo/releases/tag/v1.2.3",
        "https://github.com/felixfelix-bot/foo/blob/master/docs/x.md",
        "https://github.com/felixfelix-bot/foo/commit/abc1234",
        "https://raw.githubusercontent.com/felixfelix-bot/foo/master/x.txt",
        "https://blossom.example.com/deadbeef.mp4",
        "https://example.org/shot.png",
    ):
        assert ge.RE_DELIVERED_URL.search(url), f"not credited: {url}"


def test_prose_is_not_evidence():
    for prose in (
        PROSE_ONLY,
        "Done. Posted it.",
        "I delivered the video to the PR and everything is complete.",
        "The artifact has been published.",
    ):
        assert not ge.RE_DELIVERED_URL.search(prose), f"wrongly credited: {prose}"


# ── the predicate wired into the tier (not just defined) ────────────────────

def test_delivery_card_with_a_published_url_passes():
    r = _evaluate(f"{PR_COMMENT_URL}\n{SCAN_CLEAN}")
    assert "delivery_evidence" in r["passed"], r
    assert r["verdict"] == "pass", r


def test_delivery_card_with_prose_only_blocks():
    r = _evaluate(f"{PROSE_ONLY}\n{SCAN_CLEAN}")
    assert "delivery_evidence" in r["missing"], r
    assert r["verdict"] == "block", r


# ── the spec routes the tags (the half that was missing in PR #10) ──────────

def test_spec_defines_the_delivery_tier():
    tier = SPEC["tiers"].get("delivery")
    assert tier, "no `delivery` tier in gates.default.json"
    assert tier["require"] == ["delivery_evidence", "secrets_clean"], tier
    assert tier["enforce"] == "block", tier
    for absent in ("ci_evidence", "tests_green", "consolidated"):
        assert absent not in tier["require"], f"{absent} is unsatisfiable here"


def test_spec_routes_delivery_tags():
    routes = SPEC["tag_tiers"]
    for tag in ("delivery-only", "delivery", "evidence-only", "evidence"):
        assert routes.get(tag) == "delivery", f"{tag} -> {routes.get(tag)}"


# ── the bug D-144 exists to kill: the code tier on a delivery-only card ─────

def test_code_tier_on_a_delivery_card_is_unsatisfiable():
    """Guards the premise: with no `delivery` tier, a delivery-only card is
    asked for code evidence it cannot produce. If this ever passes cheaply, the
    delivery tier has become decorative."""
    r = _evaluate(f"{PR_COMMENT_URL}\n{SCAN_CLEAN}", tier="code")
    assert r["verdict"] == "block", r
    assert set(r["missing"]) & {"ci_evidence", "tests_green", "consolidated"}, r
