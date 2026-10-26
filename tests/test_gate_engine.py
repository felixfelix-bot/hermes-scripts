#!/usr/bin/env python3
"""Unit tests for gate_engine.py — Gate 2.10 pcb_review + pcb_consult (TDD).

Run:
    python3 -m unittest discover -s tests -v
    python3 -m pytest tests/test_gate_engine.py -q
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gate_engine as ge


# Minimal gates fixture with the families we need for cross-family checks.
GATES_FIXTURE = {
    "version": 1,
    "default_tier": "code",
    "tiers": {
        "code": {
            "enforce": "block",
            "coverage_threshold": 80,
            "require": [
                "tests_green",
                "ci_evidence",
                "cold_cross_family_review",
                "pushed_or_consolidated",
                "pcb_review",
                "pcb_consult",
            ],
            "review": {"required": True, "cross_family": True},
        },
        "docs": {"enforce": "advisory", "coverage_threshold": 0,
                 "require": ["pushed_or_consolidated"],
                 "review": {"required": False, "cross_family": False}},
    },
    "tag_tiers": {"docs": "docs", "light": "docs"},
    "families": {
        "glm": "zhipu",
        "kimi": "moonshot",
        "deepseek": "deepseek",
        "gpt": "openai",
    },
}


def make_artifact(min_bytes: int = 100, suffix: str = ".txt") -> str:
    """Create a temp file of at least min_bytes and return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, b"x" * min_bytes)
    os.close(fd)
    return path


class TestPcbTagTrigger(unittest.TestCase):
    def test_pcb_tag_line_triggers(self):
        for word in ("pcb", "schematic", "board", "kicad", "layout", "fab", "gerber"):
            with self.subTest(word=word):
                self.assertTrue(ge.pcb_tagged(f"tags: {word}\nbody"))

    def test_non_pcb_tag_line_does_not_trigger(self):
        self.assertFalse(ge.pcb_tagged("tags: docs\nbody"))

    def test_first_tags_line_only(self):
        # first line is non-pcb, second is pcb -> not triggered (first line rule)
        self.assertFalse(ge.pcb_tagged("tags: docs\ntags: pcb\nbody"))

    def test_pcb_word_in_body_without_tags_line_not_trigger(self):
        self.assertFalse(ge.pcb_tagged("This is about pcb layout\n"))

    def test_pcb_tag_in_result_or_body(self):
        self.assertTrue(ge.pcb_tagged("tags: kicad\n"))
        self.assertTrue(ge.pcb_tagged("body here\ntags: gerber\n"))


class TestPcbReviewValidation(unittest.TestCase):
    def _valid(self, verdict="APPROVED", reviewer_model="kimi-k2.7-code",
               reviewer_profile="pcb-reviewer", artifact=None,
               drc="0", erc="0", netlist_parity="ok", **overrides):
        artifact = artifact or make_artifact()
        if artifact is not None and not isinstance(artifact, str):
            artifact = str(artifact)
        base = dict(
            verdict=verdict,
            reviewer_model=reviewer_model,
            reviewer_profile=reviewer_profile,
            artifact=artifact,
            drc=drc,
            erc=erc,
            netlist_parity=netlist_parity,
        )
        base.update(overrides)
        return base

    def setUp(self):
        self._cleanup_paths = []

    def tearDown(self):
        for p in self._cleanup_paths:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def _artifact(self, content: str | bytes = None, suffix: str = ".txt"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        self._cleanup_paths.append(path)
        if content is not None:
            data = content if isinstance(content, bytes) else content.encode()
            os.write(fd, data)
        os.close(fd)
        return path

    def test_valid_pcb_review_passes(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertIsNone(r.get("reason"))

    def test_missing_pcb_review_line_fails(self):
        r = ge.pcb_review_present("no evidence", author_model="glm-5.3", gates=GATES_FIXTURE)
        self.assertFalse(r["ok"])
        self.assertIn("missing pcb_review evidence line", r["reason"])

    def test_bad_verdict_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(verdict="PASS", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("verdict", r["reason"])

    def test_tier_alias_reviewer_model_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(reviewer_model="tier/schematic-review", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("reviewer_model", r["reason"])
        self.assertIn("tier", r["reason"])

    def test_repeating_tier_name_reviewer_model_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(reviewer_model="schematic-review", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("reviewer_model", r["reason"])

    def test_missing_artifact_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(artifact="/nonexistent/path.md")),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("artifact", r["reason"])

    def test_too_small_artifact_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(artifact=self._artifact("short"))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("artifact", r["reason"])

    def test_url_artifact_passes(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(artifact="https://example.com/review.md")),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertTrue(r["ok"], r.get("reason"))

    def test_non_integer_drc_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(drc="n/a", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("drc", r["reason"])

    def test_placeholder_erc_fails(self):
        for val in ("-", "?", "TBD", "", "n/a"):
            with self.subTest(erc=val):
                r = ge.pcb_review_present(
                    text=ge._format_pcb_review(**self._valid(erc=val, artifact=self._artifact("x" * 100))),
                    author_model="glm-5.3",
                    gates=GATES_FIXTURE,
                )
                self.assertFalse(r["ok"], f"erc={val!r} should fail")
                self.assertIn("erc", r["reason"])

    def test_netlist_parity_garbage_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(netlist_parity="nope", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("netlist_parity", r["reason"])

    def test_diff_netlist_parity_passes(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(netlist_parity="diff:3", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertTrue(r["ok"], r.get("reason"))

    def test_wrong_reviewer_profile_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(reviewer_profile="pcb-consultant", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("reviewer_profile", r["reason"])

    def test_same_family_reviewer_as_author_fails(self):
        r = ge.pcb_review_present(
            text=ge._format_pcb_review(**self._valid(reviewer_model="glm-5.3", artifact=self._artifact("x" * 100))),
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("family", r["reason"])


class TestPcbConsultValidation(unittest.TestCase):
    def _valid_consult(self, reviewer_model="deepseek/deepseek-v4-flash", **overrides):
        artifact = make_artifact()
        self.addCleanup(os.unlink, artifact)
        base = dict(
            verdict="APPROVED",
            reviewer_model=reviewer_model,
            reviewer_profile="pcb-consultant",
            artifact=artifact,
        )
        base.update(overrides)
        return base

    def test_valid_pcb_consult_passes(self):
        r = ge.pcb_consult_present(
            text=ge._format_pcb_consult(**self._valid_consult()),
            author_model="glm-5.3",
            review_model="kimi-k2.7-code",
            gates=GATES_FIXTURE,
        )
        self.assertTrue(r["ok"], r.get("reason"))

    def test_consultant_same_family_as_reviewer_fails(self):
        r = ge.pcb_consult_present(
            text=ge._format_pcb_consult(**self._valid_consult(reviewer_model="kimi-k3")),
            author_model="glm-5.3",
            review_model="kimi-k2.7-code",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("reviewer family", r["reason"].lower())

    def test_consultant_same_family_as_author_fails(self):
        r = ge.pcb_consult_present(
            text=ge._format_pcb_consult(**self._valid_consult(reviewer_model="glm-5.3")),
            author_model="glm-5.3",
            review_model="kimi-k2.7-code",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("author family", r["reason"].lower())

    def test_consult_tier_alias_fails(self):
        r = ge.pcb_consult_present(
            text=ge._format_pcb_consult(**self._valid_consult(reviewer_model="tier/schematic-review")),
            author_model="glm-5.3",
            review_model="kimi-k2.7-code",
            gates=GATES_FIXTURE,
        )
        self.assertFalse(r["ok"])
        self.assertIn("tier", r["reason"])


class TestPcbEvaluateIntegration(unittest.TestCase):
    def test_tagged_card_with_no_lines_fails(self):
        res = ge.evaluate(
            tier="code",
            text="result",
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
            pcb_tagged=True,
            pcb_review=None,
            pcb_consult=None,
        )
        self.assertIn("pcb_review", res["missing"])
        self.assertIn("pcb_consult", res["missing"])
        self.assertEqual(res["verdict"], "block")

    def test_tagged_card_with_both_valid_passes(self):
        artifact = make_artifact()
        self.addCleanup(os.unlink, artifact)
        review = ge._format_pcb_review(
            verdict="APPROVED",
            reviewer_model="kimi-k2.7-code",
            reviewer_profile="pcb-reviewer",
            artifact=artifact,
            drc="0",
            erc="0",
            netlist_parity="ok",
        )
        consult = ge._format_pcb_consult(
            verdict="APPROVED",
            reviewer_model="deepseek/deepseek-v4-flash",
            reviewer_profile="pcb-consultant",
            artifact=artifact,
        )
        res = ge.evaluate(
            tier="code",
            text=f"{review}\n{consult}\n",
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
            pcb_tagged=True,
            pcb_review=ge.pcb_review_present(f"{review}\n", "glm-5.3", GATES_FIXTURE),
            pcb_consult=ge.pcb_consult_present(f"{consult}\n", "glm-5.3", "kimi-k2.7-code", GATES_FIXTURE),
        )
        self.assertNotIn("pcb_review", res["missing"])
        self.assertNotIn("pcb_consult", res["missing"])

    def test_untagged_card_is_unaffected(self):
        res = ge.evaluate(
            tier="code",
            text="pushed_or_consolidated: pr https://github.com/felixfelix-bot/x/pull/1\n",
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
            pcb_tagged=False,
        )
        self.assertNotIn("pcb_review", res["missing"])
        self.assertNotIn("pcb_consult", res["missing"])

    def test_body_text_cannot_self_satisfy(self):
        # Body-like text in the evidence blob must not count as evidence.
        # evaluate() uses the supplied pcb_review/pcb_consult results; if those
        # are None the gate fails even when the raw text contains the words.
        body = "pcb_review: verdict=APPROVED reviewer_model=kimi-k2.7-code ..."
        res = ge.evaluate(
            tier="code",
            text=body,
            author_model="glm-5.3",
            gates=GATES_FIXTURE,
            pcb_tagged=True,
            pcb_review=None,
            pcb_consult=None,
        )
        self.assertIn("pcb_review", res["missing"])
        self.assertIn("pcb_consult", res["missing"])


if __name__ == "__main__":
    unittest.main()
