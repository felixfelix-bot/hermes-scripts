#!/usr/bin/env python3
"""gate_engine.py — D-128 tiered quality-gate evaluation (pure + CLI).

Classifies a task into a tier (code | docs), then evaluates the completion
evidence against the tier's required gates:

  tests_green            a passing test/smoke run is recorded
  cold_cross_family_review  an APPROVED review by a *different* model family
  pushed_or_consolidated a push / PR / consolidation branch is recorded
  coverage               optional coverage % >= threshold

`code` tier is enforced (block); `docs` is advisory. Unknown ⇒ code (fail safe).

Pure functions are unit-tested (tests/test_gate_engine.py). CLI:
  gate_engine.py evaluate --board B --task T [--json]
  gate_engine.py classify --board B [--tags a,b] [--json]
  gate_engine.py family --model glm-5.3
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

def _resolve_hermes() -> Path:
    """Resolve the Hermes data root, tolerating HERMES_HOME pointing at a profile.

    A profile dir (``<root>/profiles/<name>``) would otherwise send BOARDS and
    GATES to the wrong place — the 'gate never fires' bug. If HERMES_HOME looks
    like a profile, fall back to the ``.hermes`` root.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        p = Path(env).expanduser()
        if "profiles" in p.parts:
            i = p.parts.index("profiles")
            root = Path(*p.parts[:i]) if i else Path(os.sep)
            return root
        return p
    return Path(os.path.expanduser("~/.hermes"))


HERMES = _resolve_hermes()
BOARDS = HERMES / "kanban" / "boards"
PROFILES = HERMES / "profiles"
GATES = HERMES / "bot" / "gates.json"
DEFAULT_GATES = Path(__file__).resolve().parent / "gates.default.json"
REPO_GATES = Path(__file__).resolve().parents[2] / "scripts" / "governance" / "gates.default.json"


def live_drift_paths(state_path: str | Path | None = None) -> list[str]:
    """Unmanaged live edits to authority=repo files (Phase L / L4).

    Written by the repo-drift-check timer (`repo_drift_state.json`). Absent or
    unreadable => [] (fail-open: never block merely because the auditor hasn't
    run). Non-empty => a card cannot complete until live matches origin/master.
    """
    p = Path(os.path.expanduser(str(state_path))) if state_path else Path(
        os.path.expanduser(os.environ.get("REPO_DRIFT_STATE")
                           or str(HERMES / "bot" / "repo_drift_state.json")))
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    d = data.get("drifted") if isinstance(data, dict) else None
    return [str(x) for x in d] if isinstance(d, list) else []

# Fail-closed sentinel: an unreadable/invalid spec must block, never pass.
GATES_FAIL_CLOSED = {
    "version": 1,
    "default_tier": "code",
    "__spec_error__": True,
    "tiers": {
        "code": {"enforce": "block",
                 "require": ["gates_spec", "tests_green", "ci_evidence",
                             "cold_cross_family_review", "pushed_or_consolidated"]},
        "docs": {"enforce": "advisory", "require": ["pushed_or_consolidated"]},
    },
    "families": {},
}

# CI-evidence contract (D-132). Task evidence must carry one canonical line:
#   ci_evidence repo=<owner/repo> head=<sha> [ref=<ref>]
RE_CI_LINE = re.compile(r"^\s*ci[_ ]evidence\b.*$", re.I | re.M)
RE_CI_REPO = re.compile(r"\brepo\s*[:=]\s*([A-Za-z0-9._/@-]+)", re.I)
RE_CI_HEAD = re.compile(r"\bhead\s*[:=]\s*([0-9a-fA-F]{7,64})\b")
RE_CI_REF = re.compile(r"\bref\s*[:=]\s*(\S+)")

CI_GREEN, CI_RED, CI_NO_RESULTS, CI_UNAVAILABLE = (
    "green", "red", "no_results", "unavailable")

# process-lifetime cache keyed by (bin, repo, head, ref)
_CI_CACHE: dict = {}

RE_TEST = [
    re.compile(r"\b\d+\s+passed\b", re.I),
    re.compile(r"\btests?\s+pass(?:ed|ing)?\b", re.I),
    re.compile(r"\ball\s+tests?\s+green\b", re.I),
    re.compile(r"\bexit(?:_code)?[\s:=]+0\b", re.I),
    re.compile(r"\b(?:pytest|npm test|cargo test|go test|bun test)\b.*\b(?:ok|pass|green)\b", re.I),
]
RE_COVERAGE = re.compile(r"(?:coverage[\s:=]*|)(\d{1,3}(?:\.\d+)?)\s*%", re.I)
RE_APPROVE = re.compile(r"\bAPPROVED\b")
RE_MODEL = re.compile(r"(?:reviewer[_\s-]*model|model)\s*[:=]\s*([A-Za-z0-9._:\-/]+)", re.I)
RE_PUSH = re.compile(
    r"(?:\bpushed?\b|\bpush(?:ed)? to\b|pull request|\bPR\s*#?\d+|"
    r"\bconsolidat(?:ed|ion)\b|\bgithub\.com/\S+|\branch\b.*\bpushed\b)", re.I)

# secrets_clean (§19.2): the task must attest a clean secret scan AND the gate
# independently re-scans the task evidence + attachments. Attestation line:
#   secret-scan: clean (gitleaks 8.21.2)   |   secret-scan: clean (regex fallback)
RE_SECRET_CLEAN = re.compile(r"secret-scan:\s*clean", re.I)
RE_SECRET_TOOL = re.compile(r"secret-scan:\s*clean\s*\(([^)]*)\)", re.I)

# High-signal detectors, kept in-code (not read from disk) so the gate works no
# matter where gate_engine is installed. The keyword-anchored 64-hex rule is the
# §22.5 gap-closer (bare 64-hex keys otherwise match nothing).
SECRET_DETECTORS = (
    re.compile(r"nsec1[a-z0-9]{20,}"),
    re.compile(r"sk-(?:or-v1-)?[A-Za-z0-9-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE" r" KEY"),
    re.compile(r"(?i)(?:nsec[_ -]?hex|private[_ -]?key|mnemonic|secret[_ -]?key)"
               r"[^\n:=`]{0,15}?[:=]\s*[\"'` ]?[0-9a-f]{64}\b"),
)
# Known non-secret placeholders / all-zero keys (allowlisted by value).
SECRET_ALLOWLIST = re.compile(
    r"nsec1(?:mock|qqqqq|deleg|coord|local|u70xp|vl029)[0-9A-Za-z]*"
    r"|\b0{64}\b", re.I)


def _read(p, d=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _to_epoch(v) -> int:
    """Coerce a task timestamp to unix seconds.

    Boards are inconsistent: integers, floats, and ISO-8601 text all appear.
    Unknown/empty values yield 0.
    """
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        pass
    try:
        return int(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def enforce_since_ts(gates: dict | None = None) -> int:
    """Global grandfather cutoff (unix secs). 0 = enforce for all tasks.

    Resolution order: spec top-level ``enforce_since_ts`` then the
    ``GATES_ENFORCE_SINCE_TS`` env var (set by the cron wrapper from
    inventory). Tasks whose effective time predates the cutoff are exempt
    from *all* gates (D-132 grandfather addendum).
    """
    g = gates or load_gates()
    v = g.get("enforce_since_ts")
    if v is None:
        v = os.environ.get("GATES_ENFORCE_SINCE_TS")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# Gates that can never be dropped from the code tier, even by an edited spec.
# `ci_evidence` is non-negotiable (D-132): a code task's review must consume CI.
# `review_artifact` is non-negotiable (D-128 §20): a review must be written to a
# file/attachment so a truncated chat message never forces a token-wasting re-run.
MANDATORY_CODE_GATES = ("ci_evidence", "review_artifact", "review_published",
                        "consolidated", "review_benchmark_floor",
                        "secrets_clean", "pr_branch_naming", "no_live_drift")

RE_FULL_REVIEW = re.compile(r"FULL_REVIEW\s*[:=]\s*(\S+)", re.I)

# delivery_evidence (D-144): the delivery-tier counterpart of ci_evidence.
# A code card proves it passes CI; a delivery-only card has no code to prove, so
# it must prove the artifact PUBLISHED somewhere a human can open — a PR comment
# or review, a release, a blob/commit, or a raw artifact URL. Prose asserting
# "delivered" is not evidence.
#
# HARDENED 2026-10-06 (cold cross-family review of PR #10, merged 2ac12fd). The
# first version was ONE regex applied to the AGGREGATE evidence blob (result +
# the last 30 comments) and it credited three things it must not:
#   * the card's own INSTRUCTIONS — "post the evidence to <PR URL>" is a
#     sentence about what to do, and the card D-144 exists for is exactly that
#     shape, so the gate was satisfied by the URL in its own task text (the
#     `/pull/1` incident card was one dispatch away from false credit);
#   * a FAILURE report — "could not post to …/pull/12" matched `/pull/\d+`;
#   * `\S+\.(?:mp4|png|webm)` unanchored, so `http://a.png` and any bare
#     filename matched, and `…invalid/x/blob/y` credited the `/blob/` branch.
# The shape now: a host-anchored URL with a real path, on a host that could
# resolve, evaluated ONLY against `delivery_evidence_text()`.
RE_DELIVERY_URL = re.compile(
    r"https?://(?P<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
    r"(?::\d+)?(?P<path>/[^\s<>\"')\]]*)", re.I)

# The path shapes only a PUBLISHED artifact carries.
RE_DELIVERY_PATH = re.compile(
    r"/pull/\d+|/releases?(?:/|$)|/blob/|/commit/|/issues?/\d+|/discussions?/\d+"
    r"|/\S+\.(?:mp4|webm|mov|mkv|png|jpe?g|webp)(?:$|[?#])", re.I)

# Hosts that can never name a published artifact: the RFC-2606 documentation
# names (example/invalid/localhost), the RFC-6761 special-use TLDs, and a host
# whose last label is a FILE EXTENSION — `https://a.png/x.mp4` is the tell, and
# it is the class the review's `http://a.png` counterexample belongs to. Same
# fail-safe rule RE_CONSOLIDATED already applies to merge URLs.
RE_PLACEHOLDER_HOST = re.compile(
    r"(?:^|\.)(?:example|invalid|localhost)(?:\.|$)"
    r"|\.(?:local|test|internal)$"
    r"|\.(?:mp4|webm|mov|mkv|png|jpe?g|webp|gif|txt|md)$", re.I)

# A URL carrying a format placeholder is a documented SHAPE, not a link.
RE_TEMPLATED_URL = re.compile(r"%s|\$\{|<[A-Za-z_]|\{[A-Za-z_]|\.\.\.")

# Raw-content hosts: the artifact IS the path, so there is no /blob/-style
# marker to look for — only "a path deep enough to name a file".
RE_RAW_CONTENT_HOST = re.compile(
    r"^(?:raw\.githubusercontent\.com|raw\.github\.com|"
    r"objects\.githubusercontent\.com)$", re.I)


# A URL quoted inside a FAILURE report is not evidence: "Blocked: could not post
# to …/pull/12 — the token lacks write access" names exactly the URL a success
# would, so the URL shape alone cannot tell them apart — the LINE the URL sits
# on has to be read. Scoped to that one line on purpose, so a refusal narrated
# elsewhere in a long comment cannot disarm a real assertion (same rule as
# RE_CONSOLIDATED).
RE_DELIVERY_NEGATION = re.compile(
    r"\b(?:could ?n[o']?t|could not|cannot|can'?t|unable to|failed to|failure|"
    r"blocked|denied|refused|no write access|"
    r"not (?:posted|published|delivered|uploaded|attached|committed))\b", re.I)


def _line_around(text: str, pos: int) -> str:
    """The single line containing ``pos`` (no trailing newline)."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:] if end == -1 else text[start:end]


def delivery_evidence_present(text: str) -> bool:
    """True when ``text`` carries a URL that proves an artifact was PUBLISHED.

    Regex-only by design: this runs per tick for every board and must never make
    a network call. Residual limit, stated not implied — a hand-typed
    bogus-but-plausible host (``https://acme-notreal.com/x.mp4``) is not
    detectable without a DNS/HTTP lookup and is NOT caught here. What IS caught
    is the classes the review falsified: a filename with no host at all, a host
    that cannot resolve, a templated URL, and a URL quoted inside the failure
    report that says it was NOT posted.
    """
    text = text or ""
    for m in RE_DELIVERY_URL.finditer(text):
        if RE_TEMPLATED_URL.search(m.group(0)):
            continue
        if RE_DELIVERY_NEGATION.search(_line_around(text, m.start())):
            continue
        host = m.group("host") or ""
        path = m.group("path") or ""
        if RE_PLACEHOLDER_HOST.search(host):
            continue
        if RE_RAW_CONTENT_HOST.match(host) and path.count("/") >= 2:
            return True
        if RE_DELIVERY_PATH.search(path):
            return True
    return False


def delivery_evidence_text(conn: sqlite3.Connection, task_id: str,
                           result: str | None, completed_at) -> str:
    """The surfaces a card CANNOT pre-fill: its own RESULT, plus the comments
    written AFTER it completed.

    Scoping is the fix for the review's first finding. The aggregate evidence
    blob (``evidence_text``) leads with the card's own instructions, so a card
    whose text says "post the evidence to <PR URL>" credited the delivery gate
    with the sentence telling the worker what to do — which is precisely the
    card D-144 was written for. A pre-completion comment is instruction surface
    or work-in-progress; the RESULT, and any comment posted after completion,
    are the card's claim that the artifact exists.
    """
    parts = [result or ""]
    cut = _to_epoch(completed_at)
    if cut:
        try:
            for body, created in conn.execute(
                    "select coalesce(body,''), created_at from task_comments"
                    " where task_id=?", (task_id,)):
                c = _to_epoch(created)
                if c and c > cut:
                    parts.append(body or "")
        except sqlite3.Error:
            pass
    return "\n".join(parts)

# A *published* review: a concrete GitHub PR review or comment URL for the PR
# under review (e.g. .../pull/1318 or .../pull/1318#issuecomment-5691681965).
RE_GH_PR_URL = re.compile(
    r"github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+(?:#[\w-]+)?", re.I)

# Consolidated (done == landed): a merge SHA into the default branch, or (fork /
# third-party repos) a single open upstream PR URL (D-116). A bare push no longer
# satisfies completion.
#
# t_d94839f1: credited by an ILLUSTRATIVE line. The pattern used to be
# `consolidated\s*[:=]\s*(?:merged?\s+[0-9a-f]{7,40}|pr\s+https?://\S+)`, matched
# ANYWHERE in the evidence blob, so a card that merely DOCUMENTED the shape of
# the evidence it intended to produce credited the fleet's "done == landed"
# gate. hermes-for-friends:t_4112c589 comment 169 quoted, as an illustration of
# what option (C) would emit:
#     consolidated: pr https://github.com/OpenTollGate/…/pull/<N>
# No such PR existed, and the next `gate_engine.py evaluate` passed
# `consolidated` for that card (its sibling `pr_branch_naming` stayed missing —
# that gate had already been hardened the same way in t_7589d2c1/t_63f0ddb9).
#
# Two rules, both empirical over the live fleet (174 boards / 5972 tasks,
# 2026-09-19; every genuine evidence line on every board still credits):
#   1. a CONCRETE value: merge sha hex, or a URL that is really
#      `host/owner/repo/pull/<digits>` on a host that can host a PR. A bare
#      host, a `%s`/`{{var}}`/`<var>`/`N` template, or example.com/.invalid/
#      localhost is a documented shape, not a landed PR.
#   2. not in an ILLUSTRATIVE context: the line itself, the line immediately
#      above it, or the line immediately below it carrying an illustration
#      marker ("as an illustration", "Example evidence line:", "(hypothetical)",
#      …). Scoped to the adjacent lines on purpose: the wording of a refusal
#      elsewhere in a long comment must not disarm a real assertion.
RE_CONSOLIDATED = re.compile(
    r"consolidated\s*[:=]\s*(?:merged?\s+[0-9a-f]{7,40}"
    r"|pr\s+https?://[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}"
    r"/[^\s/]+/[^\s/]+/(?:pull|pulls)/\d+)", re.I)
# A template variable / documented shape standing where a concrete value must
# be (`<N>`, `<slug>`, `{{org}}`, `%s`, `PR_URL`, `…/pull/N`).
RE_CONSOLIDATED_PLACEHOLDER = re.compile(
    r"[<>{}]|%s|%\(|\bPR_URL\b|\bN/?A\b|/N\b|/N/|\bplaceholder\b|\bTODO\b"
    r"|\bslug\b|\bsha\b|\bSHA\b|\bHEAD\b|\bnumeric\b", re.I)
# A host that cannot host the PR being cited.
RE_CONSOLIDATED_FAKE_HOST = re.compile(
    r"https?://(?:example\.(?:com|org|net)|localhost|[\w.-]*\.(?:invalid|test|example))"
    r"(?::\d+)?(?:/|$)", re.I)
# The illustration markers. Deliberately NOT a bare `example\b`: real accepted
# cards write "(D-116; not a quoted example)" next to genuine evidence.
RE_CONSOLIDATED_ILLUSTRATIVE = re.compile(
    r"illustrat\w*|hypothetical\w*|placeholder|pretend\w*|made[- ]up|fictional"
    r"|imaginary|\bsample\b|as an example|example\s+(?:only|evidence|line)"
    r"|^\s*example\b|e\.g\.|for\s+instance|would\s+(?:produce|be|look|read)"
    r"|should\s+(?:be|read)|non-?existent|not\s+real|the\s+shape\s+of"
    r"|\btemplate\b|intend\w*\s+to|documenting|to\s+be\s+produced", re.I)

# Review benchmark floor: the review must record which model produced it
# (D-115 reviewer_model) and, optionally, its family tier.
RE_REVIEWER_MODEL = re.compile(
    r"reviewer[_\s-]*model\s*[:=]\s*([A-Za-z0-9._:/@-]+)", re.I)
RE_REVIEW_TIER = re.compile(r"review[_\s-]*tier\s*[:=]\s*(tier/[A-Za-z0-9._-]+)", re.I)

# pr_branch_naming (D-135 §23): when a task references an open PR, its branch
# must be `pr/<slug>`; evidence line `pr-branch: pr/<slug>`.
RE_PR_REF = re.compile(
    r"github\.com/\S+/(?:pull|pulls)/\d+|\bPR\s*#?\d+|\bpull request\b", re.I)
RE_PR_BRANCH = re.compile(
    r"pr-branch\s*[:=]\s*`?(pr/(?=[A-Za-z0-9])[A-Za-z0-9._/-]*[A-Za-z0-9/])",
    re.I)
# A `pr-branch:` token is an ASSERTION only when it is a *bare, line-initial*
# evidence line. A quotation is not a claim, and neither is prose that declines
# to make one. plebeian:t_a27d2142 comment 175 read "I am not writing a
# <inline-code pr-branch token>" and a raw prose search credited
# `pr_branch_naming` on a card whose branch was not `pr/<slug>`
# (plebeian:t_7589d2c1, D-135 §23).
#
# RE_ASSERT_LEAD is the ONLY thing allowed to precede the key on its line:
# emphasis/bullet decoration, list numbering, a date, or a PR reference.
# Refusal prose ("I am not writing …", "no … line is asserted", "I decline
# to …") cannot be built out of those tokens, so no phrasing of a refusal can
# credit the gate — while every accepted form real cards already use stays
# green:
#   pr-branch: pr/<slug>
#   pr-branch: pr/<slug> (head <sha>, base <ref>, …)
#   - **pr-branch: pr/<slug>** — <prose>
#   PR #4 pr-branch: pr/<slug>
# `>` (blockquote) and `#` (heading) are deliberately NOT allowed: they are the
# markdown idioms for *quoting someone else*/describing, not asserting, so
# "> pr-branch: pr/other-card-slug — quoted from t_x" and
# "# pr-branch: pr/in-a-heading" must not credit this card's floor gate.
# Deliberate trade-off: a genuine assertion written *inside* a line of prose
# ("Use `x` then pr-branch: pr/y") is NOT credited. The gate wants an evidence
# line, not a sentence; the fix for an honest card is to put the line on its
# own line. Verified against every task on every board (5878) — see
# tests/test_gate_engine.py.
RE_ASSERT_LEAD = re.compile(
    r"^(?:[\s*_+-]+|\d+[.)]|\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?"
    r"|\bPR\s*#?\d+\b|\bpull\s+request\b)+",
    re.I)
RE_FENCE = re.compile(r"^\s*(?:```|~~~)")
RE_CODE_SPAN = re.compile(r"`+[^`]*`+")
RE_URL = re.compile(r"https?://\S+")
# A branch/ref-like token: at least one letter either side of a slash, so
# `+51/-0` and `2026-09-17` are not refs but `fix/foo` and `pr/x` are.
RE_REF_TOKEN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
RE_HAS_LETTER = re.compile(r"[A-Za-z]")
# An `N/A` value credits nothing, even when a real-looking slug shares the line.
RE_PR_BRANCH_NA = re.compile(
    r"pr-branch\s*[:=]\s*(?:n\s*/?\s*a\b|none\b|tbd\b|-)", re.I)
# A bare assertion followed by an explicit disclaimer ("(I decline to assert
# this)") is not an assertion. Kept narrow on purpose: real cards write
# "(D-135 §23; not a quoted example)" AFTER the token, which must stay credited.
RE_PR_BRANCH_DISCLAIMED = re.compile(
    r"(?:declin\w*|omit\w*|skip\w*|exclud\w*|refus\w*|avoid\w*)\s+(?:to\s+)?"
    r"(?:assert\w*|claim\w*|writ\w*|stat\w*|add\w*|includ\w*)"
    r"|(?:not|no|never)\s+(?:\w+\s+){0,3}?(?:asserted|claimed|written|stated|added)\b"
    # trailing QUOTATION: "— quoted from the other card", NOT the real card's
    # "...(D-135 §23; not a quoted example)", which must stay credited.
    r"|\bquot(?:ed|ing)\s+(?:from|verbatim|upstream|elsewhere|the\s+spec"
    r"|docs|the\s+other|another|a\s+different)\b"
    # trailing HYPOTHETICAL / non-assertion marker. "example" is deliberately
    # absent: the real accepted form on t_d43504b1 ends
    # "(...; not a quoted example)".
    r"|\(\s*(?:proposed|planned|fictional|imaginary|sample|placeholder"
    r"|hypothetical|illustrative|pretend\w*|withheld|declined|refused"
    r"|superseded)\b"
    r"|\b(?:hypothetical\w*|illustrative|placeholder|pretend\w*|made[- ]up"
    r"|non-?existent|not\s+real)\b"
    # suffixes that contradict the assertion without naming another branch
    # (defense in depth; the structural half is _names_other_ref)
    r"|\bnot\s+the\s+(?:real|actual|true)\b"
    r"|\b(?:real|actual|true)\s+(?:head|branch|ref|slug)\b"
    r"|\b(?:actual|real)\s+head\s+branch\b"
    r"|\be\.g\."
    r"|\bfor\s+example\b|\bwould\s+be\b|\bshould\s+be\b|\bas\s+quoted\b"
    r"|\bquot\w*\s+from\b",
    re.I)

# playbook_check (D-143): a card that changes a playbook or a fleet role must
# carry the pre-merge playbook check's own verdict. scripts/ci/check_playbooks.sh
# runs the SAME ansible `--check` the deploy enforces (scripts/deploy.sh has a
# mandatory --check pre-flight) and prints `playbook_check: pass`. Without this,
# a broken task (bad loop-item shape, a 404 URL, retries/delay inside module
# args) is only discovered when a deploy aborts the WHOLE playbook — blocking
# every governance change behind it. `--syntax-check` cannot see any of them.
# Conditional (only when the card references a playbook/role change), exactly
# like pr_branch_naming: unrelated code work is never blocked.
RE_PLAYBOOK_REF = re.compile(
    r"\bplaybook[\w.-]*\.ya?ml\b|\broles/\d+[\w-]*/|\broles/[\w-]+/tasks/", re.I)
RE_PLAYBOOK_OK = re.compile(r"playbook_check\s*[:=]\s*pass\b", re.I)


def playbook_check_present(text: str) -> bool:
    """True when the card carries the pre-merge playbook check's own verdict.

    Fenced code is stripped first so a *quoted* evidence line cannot credit the
    gate (same hardening as pr_branch_naming / consolidated).
    """
    return bool(RE_PLAYBOOK_OK.search(strip_code_fences(text or "")))


def _names_other_ref(suffix: str) -> bool:
    """True when the text AFTER an asserted slug names a *different* branch/ref.

    Structural half of the suffix rule (cross-family review round 3): the
    surviving form of the original t_a27d2142 defect is the assertion left in
    place with the truth appended — `pr-branch: pr/fake (actual head branch is
    fix/foo)`. No denylist of phrasings can cover that, because the deciding
    token is the *other branch name*, not the surrounding prose. Naming one is
    therefore a rejection by construction, whatever the wording around it.

    URLs are exempt (a card may cite the PR a metadata clause refers to), and a
    `+51/-0` diffstat is not a ref (both sides need a letter).
    """
    masked = RE_URL.sub(" " * 1, suffix or "")
    for m in RE_REF_TOKEN.finditer(masked):
        lhs, rhs = m.group(0).split("/", 1)
        if RE_HAS_LETTER.search(lhs) and RE_HAS_LETTER.search(rhs):
            return True
    return False



def _balanced_fence_lines(lines: list[str]) -> set[int]:
    """Line indices inside a *balanced* ``` / ~~~ block.

    A dangling (unclosed) fence is ignored rather than swallowing every line
    after it, so evidence aggregated from several comments cannot be blinded
    by one unrelated comment's syntax error.
    """
    idx = [i for i, ln in enumerate(lines) if RE_FENCE.match(ln)]
    inside: set[int] = set()
    for a, b in zip(idx[0::2], idx[1::2]):
        inside.update(range(a + 1, b))
    return inside


def _quoted_at(line: str, start: int) -> bool:
    """True when the key at `start` is inside a code span that began earlier.

    A span that starts AT the value (`pr-branch: `pr/x``) is a value in code,
    not a quoted assertion, and stays credited.
    """
    for s in RE_CODE_SPAN.finditer(line):
        if s.start() <= start - 1 < s.end():
            return True
    return False


def pr_branch_asserted(text: str = "") -> bool:
    """True only when `text` ASSERTS `pr-branch: pr/<slug>` (D-135 §23).

    Callers must use this instead of `RE_PR_BRANCH.search`: the pattern alone
    cannot tell an assertion from a quotation or from a sentence that declines
    the claim. The token must be line-initial (after markdown decoration, list
    numbering, a date or a PR reference), outside a balanced code fence, not a
    quoted key, not an `N/A` declaration, not disclaimed after the fact, and
    must not nominate a DIFFERENT branch in its suffix.
    """
    lines = (text or "").splitlines()
    fenced = _balanced_fence_lines(lines)
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        lead_m = RE_ASSERT_LEAD.match(line)
        lead = lead_m.end() if lead_m else 0
        for m in RE_PR_BRANCH.finditer(line):
            if m.start() != lead:
                continue
            if _quoted_at(line, m.start()):
                continue
            if RE_PR_BRANCH_NA.search(line[:m.start()]):
                continue
            suffix = line[m.end():]
            # a slug followed by `?` is a question about a slug, not an assertion
            if suffix.startswith("?"):
                continue
            if RE_PR_BRANCH_DISCLAIMED.search(suffix):
                continue
            if _names_other_ref(suffix):
                continue
            return True
    return False


def review_artifact_present(board: str, task_id: str, text: str = "") -> bool:
    """True when a non-trivial review artifact exists for the task (D-128 §20)."""
    adir = BOARDS / board / "attachments" / task_id
    if adir.is_dir():
        try:
            for f in adir.iterdir():
                if f.is_file() and f.suffix.lower() in (".md", ".txt", ".json"):
                    if f.stat().st_size >= 100:
                        return True
        except OSError:
            pass
    rep = Path(os.path.expanduser("~/reports/reviews"))
    if (list(rep.glob(f"{board}-{task_id}*")) or list(rep.glob(f"*{task_id}*"))):
        return True
    return bool(RE_FULL_REVIEW.search(text or ""))


def review_published_present(board: str, task_id: str, text: str = "") -> bool:
    """True when the review was PUBLISHED to the PR, not merely drafted.

    D-128 §20 requires a review *artifact* on disk; this stronger completion-
    integrity check additionally requires a GitHub review/comment URL for the PR
    in the task result/comments or the artifact. Closes the silent-`done` gap
    where a card completed with a local draft and nothing on the PR
    (t_0f5112de: offload-held, then marked done with no publish).
    """
    blob = text or ""
    try:
        adir = BOARDS / board / "attachments" / task_id
        if adir.is_dir():
            for f in sorted(adir.iterdir()):
                if f.is_file() and f.suffix.lower() in (".md", ".txt", ".json"):
                    try:
                        blob += "\n" + f.read_text(errors="replace")
                    except OSError:
                        continue
    except OSError:
        pass
    rep = Path(os.path.expanduser("~/reports/reviews"))
    try:
        for f in list(rep.glob(f"{board}-{task_id}*")) + list(rep.glob(f"*{task_id}*")):
            try:
                blob += "\n" + f.read_text(errors="replace")
            except OSError:
                continue
    except OSError:
        pass
    return bool(RE_GH_PR_URL.search(blob))


def consolidated_present(text: str = "") -> bool:
    """True when the task records consolidation to the default branch.

    Accepts `consolidated: merged <sha>` (our own repos) or
    `consolidated: pr <url>` (fork/third-party, D-116 single open upstream PR).
    A bare push/PR mention does NOT satisfy this gate.

    Since t_d94839f1 the value must be CONCRETE (no `<N>` / `{{var}}` / `%s` /
    example host, no bare host) and the line must not sit in an ILLUSTRATIVE
    context — an evidence line that merely documents the shape of the line the
    card intends to produce, or quotes this very defect, credits nothing. The
    context check looks at the match's line plus its immediate neighbours
    (instructions can precede or follow a fenced illustration).
    """
    lines = (text or "").splitlines()
    blanks = re.compile(r"^\s*$")
    for i, line in enumerate(lines):
        for m in RE_CONSOLIDATED.finditer(line):
            value = m.group(0)
            if RE_CONSOLIDATED_PLACEHOLDER.search(value):
                continue
            if RE_CONSOLIDATED_FAKE_HOST.search(value):
                continue
            ctx = [line]
            for step in (-1, 1):
                j = i + step
                while 0 <= j < len(lines) and (blanks.match(lines[j])
                                               or RE_FENCE.match(lines[j])):
                    j += step          # a fence marker is not context
                if 0 <= j < len(lines):
                    ctx.append(lines[j])
            if any(RE_CONSOLIDATED_ILLUSTRATIVE.search(c) for c in ctx):
                continue
            return True
    return False


_CODING_ORDER = {"economy": 0, "solid": 1, "pro": 2}
_TOOL_ORDER = {"ok": 0, "good": 1, "strong": 2}


def _load_bench_tiers() -> tuple[dict | None, dict | None]:
    """(benchmarks, tiers) from ~/.hermes/bot/*.yaml; (None, None) on failure."""
    try:
        import yaml  # noqa: PLC0415
    except Exception:
        return None, None
    bot = Path(os.path.expanduser("~/.hermes/bot"))
    try:
        bench = yaml.safe_load((bot / "model_benchmarks.yaml").read_text()) or {}
        tiers = yaml.safe_load((bot / "model_tiers.yaml").read_text()) or {}
        return bench, tiers
    except Exception:
        return None, None


def review_benchmark_floor_ok(model: str, tier_name: str | None = None
                              ) -> bool | None:
    """True when *model* meets its review tier's benchmark minima.

    Reads the model's benchmark entry (context/coding_class/tool_use) and the
    tier minima from `model_benchmarks.yaml` / `model_tiers.yaml`. Returns None
    when the files or the tier are unavailable (cannot evaluate -> skip, so an
    unprovisioned node is not wedged).
    """
    if not model:
        return False
    bench, tiers = _load_bench_tiers()
    if bench is None or tiers is None:
        return None
    models = bench.get("models") or {}
    rec = models.get(model)
    if rec is None:  # try reported_as aliases (provider-native slugs)
        for _mid, r in models.items():
            if model in (r.get("reported_as") or []):
                rec = r
                break
    if rec is None:
        return False  # no benchmark entry -> no provenance -> fail the floor
    tier = (tiers.get("tiers") or {}).get(tier_name or "")
    if not isinstance(tier, dict):
        return None  # unknown tier -> cannot evaluate
    min_cc = _CODING_ORDER.get(str(tier.get("min_coding_class", "solid")).lower(), 1)
    min_tu = _TOOL_ORDER.get(str(tier.get("min_tool_use", "good")).lower(), 1)
    min_ctx = int(tier.get("min_context", 0) or 0)
    cc = _CODING_ORDER.get(str(rec.get("coding_class", "economy")).lower(), 0)
    tu = _TOOL_ORDER.get(str(rec.get("tool_use", "ok")).lower(), 0)
    ctx = int(rec.get("context_window", 0) or 0)
    return cc >= min_cc and tu >= min_tu and ctx >= min_ctx


def scan_secrets(text: str) -> list[str]:
    """Redacted descriptors of secret-like strings found in ``text`` (§19.2).

    Independent re-scan so a fabricated ``secret-scan: clean`` line cannot pass.
    Returns ``[]`` when clean.
    """
    if not text:
        return []
    clean = SECRET_ALLOWLIST.sub("", text)
    hits = []
    for pat in SECRET_DETECTORS:
        for m in pat.finditer(clean):
            s = m.group(0)
            hits.append(f"{pat.pattern[:16]}…{s[:6]}…")
    return hits


def _attachment_text(board: str, task_id: str) -> str:
    """Concatenate text-like attachment bodies for a task (for secret scanning)."""
    adir = BOARDS / board / "attachments" / task_id
    if not adir.is_dir():
        return ""
    parts = []
    try:
        for f in sorted(adir.iterdir()):
            if f.is_file() and f.suffix.lower() in (
                    ".md", ".txt", ".json", ".log", ".diff", ".patch", ".env"):
                try:
                    parts.append(f.read_text(errors="replace"))
                except OSError:
                    continue
    except OSError:
        return ""
    return "\n".join(parts)


def _apply_floor(g: dict) -> dict:
    tiers = g.setdefault("tiers", {})
    code = tiers.get("code")
    if isinstance(code, dict):
        req = code.get("require")
        if not isinstance(req, list):
            req = []
        for gate in MANDATORY_CODE_GATES:
            if gate not in req:
                req.append(gate)
        code["require"] = req
        code["enforce"] = "block"
    return g


def _valid_spec(g: dict) -> bool:
    """A usable spec must define a 'code' tier with a non-empty require list."""
    tiers = g.get("tiers")
    if not isinstance(tiers, dict) or not isinstance(tiers.get("code"), dict):
        return False
    req = tiers["code"].get("require")
    return isinstance(req, list) and len(req) > 0


def _candidates(path: Path | str | None) -> list:
    out = []
    if path:
        out.append(Path(path).expanduser())
    out += [
        Path(os.environ.get("NGIT_GATES_SPEC", "")).expanduser()
        if os.environ.get("NGIT_GATES_SPEC") else None,
        GATES,                                              # $HERMES_HOME/bot/gates.json
        Path(os.path.expanduser("~/.hermes/bot/gates.json")),
        DEFAULT_GATES,                                      # module-adjacent
        REPO_GATES,                                         # repo checkout
    ]
    return [c for c in out if c]


def load_gates(path: Path | str = GATES) -> dict:
    """Fail-closed spec resolution (D-132).

    Tries each candidate in order and returns the first *valid* spec. If none
    parse/validate, returns ``GATES_FAIL_CLOSED`` (``__spec_error__``) so the
    caller blocks instead of silently enforcing nothing.
    """
    for cand in _candidates(path):
        g = _read(cand)
        if isinstance(g, dict) and _valid_spec(g):
            return _apply_floor(g)
    g = dict(GATES_FAIL_CLOSED)
    g["_tried"] = [str(c) for c in _candidates(path)]
    return g


def family(model: str, gates: dict | None = None) -> str:
    """Map a model id to an LLM family (D-115 cross-family check)."""
    families = (gates or {}).get("families") or load_gates().get("families", {})
    m = (model or "").lower()
    if not m:
        return "unknown"
    for key in sorted(families, key=len, reverse=True):
        if m.startswith(key) or f"/{key}" in m or f"-{key}" in m:
            return families[key]
    return m.split("-")[0].split(":")[0] or "unknown"


def profile_model(profile: str) -> str:
    cfg = PROFILES / (profile or "") / "config.yaml"
    try:
        text = cfg.read_text()
    except OSError:
        return ""
    m = re.search(r"^\s*default:\s*([A-Za-z0-9._:\-/]+)", text, re.M)
    return m.group(1).strip() if m else ""


def parse_ci_evidence(text: str) -> dict | None:
    """Parse the canonical CI-evidence line into {repo, head, ref}.

    Returns None when the contract line is absent or lacks repo/head.
    """
    m = RE_CI_LINE.search(text or "")
    if not m:
        return None
    line = m.group(0)
    repo = RE_CI_REPO.search(line)
    head = RE_CI_HEAD.search(line)
    if not repo or not head:
        return None
    ref = RE_CI_REF.search(line)
    return {
        "repo": repo.group(1),
        "head": head.group(1).lower(),
        "ref": ref.group(1) if ref else None,
    }


def ci_evidence_bin(gates: dict | None = None) -> str:
    g = gates or {}
    spec = ((g.get("tiers") or {}).get("code", {}) or {}).get("ci_evidence", {}) or {}
    return (spec.get("bin") or g.get("ci_evidence_bin")
            or os.environ.get("NGIT_CI_EVIDENCE_BIN")
            or shutil.which("ngit_ci_evidence.py")
            or str(Path(os.path.expanduser("~/.hermes/scripts/ngit_ci_evidence.py"))))


def run_ci_evidence(ev: dict, gates: dict | None = None,
                    bin_path: str | None = None,
                    timeout: int | None = None) -> dict:
    """Invoke ngit_ci_evidence.py for the exact head and classify the result.

    Exit map (see the tool): 0 green | 1 red | 2,4 no_results | 3,5 unavailable.
    Any non-green result is a failure to be reported as a missing gate.
    """
    g = gates or load_gates()
    spec = ((g.get("tiers") or {}).get("code", {}) or {}).get("ci_evidence", {}) or {}
    bin_path = bin_path or ci_evidence_bin(g)
    outer = int(timeout or spec.get("timeout") or 180)
    inner = max(5, min(outer - 10, int(spec.get("nak_timeout") or 120)))
    key = (bin_path, ev.get("repo"), ev.get("head"), ev.get("ref"))
    if key in _CI_CACHE:
        return _CI_CACHE[key]

    if not os.path.exists(bin_path) and not shutil.which(bin_path):
        res = {"verdict": CI_UNAVAILABLE, "exit": None, "workflows": [],
               "error": f"ci evidence tool not found: {bin_path}"}
        _CI_CACHE[key] = res
        return res

    cmd = [bin_path, ev["repo"], "--commit", ev["head"], "--json",
           "--require-conclusion", "--timeout", str(inner)]
    spec_limit = int(spec["limit"]) if spec.get("limit") else None
    if spec_limit:
        cmd += ["--limit", str(spec_limit)]
    if ev.get("ref"):
        cmd += ["--ref", ev["ref"]]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=outer, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        res = {"verdict": CI_UNAVAILABLE, "exit": None, "workflows": [],
               "error": str(exc)}
        _CI_CACHE[key] = res
        return res

    rc = proc.returncode
    verdict = {0: CI_GREEN, 1: CI_RED, 2: CI_NO_RESULTS, 4: CI_NO_RESULTS}.get(rc)
    if verdict is None:
        verdict = CI_UNAVAILABLE
    workflows = []
    try:
        parsed = json.loads(proc.stdout or "[]")
        if isinstance(parsed, list):
            for r in parsed:
                if isinstance(r, dict):
                    workflows.append({"workflow": r.get("workflow", ""),
                                      "conclusion": r.get("conclusion", "unknown"),
                                      "commit": r.get("commit", "")})
    except ValueError:
        pass
    res = {"verdict": verdict, "exit": rc, "workflows": workflows,
           "error": "" if verdict != CI_UNAVAILABLE else (proc.stderr or "").strip()}
    _CI_CACHE[key] = res
    return res


def _tier_defined(g: dict, name) -> bool:
    """True when ``name`` names a tier that actually exists in the spec (D-165)."""
    return isinstance(name, str) and name in (g.get("tiers") or {})


def classify_tier(board: str, tags: list[str] | None = None,
                  gates: dict | None = None) -> str:
    """Resolve the gate tier for a (board, tags) pair.

    Resolution order: ``board_tiers[board]`` → first matching ``tag_tiers[tag]``
    → ``default_tier``. A name that does not exist in ``tiers`` is IGNORED and
    the next source is tried, so a typo in the spec can never resolve to an
    empty tier (which would silently pass every gate) — D-165 hardening.
    """
    g = gates or load_gates()
    board_tier = (g.get("board_tiers") or {}).get(board)
    if _tier_defined(g, board_tier):
        return str(board_tier)
    tag_map = g.get("tag_tiers") or {}
    for t in (tags or []):
        t = t.strip().lower()
        tag_tier = tag_map.get(t)
        if _tier_defined(g, tag_tier):
            return str(tag_tier)
    default = g.get("default_tier", "code")
    return str(default) if _tier_defined(g, default) else "code"


# Tag extraction (D-137 fix). A tag declaration counts only when it is
# line-anchored AND the declared word is one of the active spec's `tag_tiers`
# keys. The previous unanchored `re.search(...)` took the FIRST `tags?:`/`tags=`
# anywhere in body||result, so a card that QUOTED source code containing such a
# field (Go `Tags: nostr.Tags{`, JSON/YAML `tags:`) captured a non-tag word,
# missed `tag_tiers`, fell through to `default_tier=code`, and applied the
# block-enforcing code-tier gates to read-only work — after which the card was
# re-queued and re-run on every gate tick (balloon/t_1359baab, 6 dispatches on
# 2026-09-18). NOTE: the quoted Go line is tab-indented, so anchoring alone does
# NOT fix this; restricting the capture to the closed tag_tiers set is what
# makes quoted code harmless. Two further holes a cold cross-family review
# (glm-5.2, PR #118) found in the first cut are closed here: a trailing gloss can
# no longer leak tier words (the value ends at the first non-tag word) and
# fenced code blocks are stripped before scanning.
RE_TAG_DECL = re.compile(r"^[ \t>*+\-#]*tags?[ \t]*[:=][ \t]*(.+)$", re.I | re.M)
# `/`, `|`, `;` are real separators in card tag lines: `tags: docs/light`
# (docs tier alias) is the most common declaration on this fleet.
RE_TAG_SEP = re.compile(r"[,\s/|;]+")
RE_FENCE = re.compile(r"(```+|~~~+)")
TAG_TRIM = "`\"'[]{}()<>.,;:"


def known_tags(gates: dict | None = None) -> set[str]:
    """Lowercased closed set of tags the active spec can classify (tag_tiers)."""
    g = gates if isinstance(gates, dict) else load_gates()
    return {str(k).strip().lower() for k in (g.get("tag_tiers") or {})}


def strip_code_fences(text: str) -> str:
    """Drop fenced code blocks so a QUOTED declaration cannot declare a tier.

    Fleet completions start with `tags: docs`, so a meta/governance card that
    pastes another card's result inside a fence would otherwise inherit its
    tier (cross-family review F2, 2026-09-18). Unfenced text is returned
    unchanged; an unterminated fence drops only its own body.
    """
    out: list[str] = []
    fence: str | None = None
    for line in (text or "").splitlines():
        m = RE_FENCE.match(line.strip())
        if fence is None:
            if m:
                fence = m.group(1)[0] * 3
                continue
            out.append(line)
        elif m and m.group(1).startswith(fence):
            fence = None
    return "\n".join(out)


def tag_tokens(text: str, known: set[str]) -> list[str]:
    """Tag words declared on a line-anchored `tags:`/`tags=` line, in order.

    A declaration is a list of words drawn from ``known`` (the spec's tag_tiers
    keys), separated by ``,``/whitespace/``/``/``|``/``;``. The value ENDS at the
    first word that is not a known tag, so a trailing gloss cannot leak tier
    words: `tags: docs/light -- read-only survey` -> ['docs', 'light'], and
    `tags: sweep-result -- read-only inventory` -> [] instead of fabricating the
    advisory recon tier (review F1). Fenced code blocks are removed first
    (F2). Returns [] when the text carries no declaration.
    """
    out: list[str] = []
    for m in RE_TAG_DECL.finditer(strip_code_fences(text)):
        for raw in RE_TAG_SEP.split(m.group(1)):
            tok = raw.strip().strip(TAG_TRIM).lower()
            if not tok:
                continue
            if tok not in known:
                break
            if tok not in out:
                out.append(tok)
    return out


def board_tags(conn: sqlite3.Connection, task_id: str,
               gates: dict | None = None) -> list[str]:
    """Best-effort tag extraction from the task result, then body (light heuristic).

    ``result`` is consulted before ``body`` so the completion message's own
    declaration outranks a body that quotes source code; only line-anchored
    declarations of a known ``tag_tiers`` key count (see ``tag_tokens``).
    """
    try:
        row = conn.execute(
            "select coalesce(result,''), coalesce(body,'') from tasks where id=?",
            (task_id,)).fetchone()
    except sqlite3.Error:
        return []
    known = known_tags(gates)
    if not known:
        return []
    for text in (row if row else ()):
        tags = tag_tokens(text or "", known)
        if tags:
            return tags
    return []


def evidence_text(conn: sqlite3.Connection, task_id: str, result: str | None) -> str:
    parts = [result or ""]
    try:
        for (body,) in conn.execute(
            "select body from task_comments where task_id=? order by created_at desc limit 30",
            (task_id,)):
            parts.append(body or "")
    except sqlite3.Error:
        pass
    return "\n".join(parts)


def _has(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns) if isinstance(patterns, list) else bool(patterns.search(text))


def evaluate(tier: str, text: str, author_model: str,
             gates: dict | None = None, ci_result: dict | None = None,
             ci_required: bool = True, review_artifact: bool | None = None,
             review_published: bool | None = None,
             consolidated: bool | None = None,
             review_benchmark_floor: bool | None = None,
             secrets_hits: list | None = None, pr_branch: bool | None = None,
             live_drift: list | None = None,
             playbook_check: bool | None = None,
             delivery_text: str | None = None) -> dict:
    """Return {tier, verdict, passed, missing, cross_family}."""
    g = gates or load_gates()
    if g.get("__spec_error__"):
        return {"tier": tier, "verdict": "block", "passed": [], "missing": ["gates_spec"],
                "cross_family": None, "author_family": family(author_model, g)}
    spec = (g.get("tiers") or {}).get(tier, {})
    require = spec.get("require", [])

    passed, missing = [], []
    if "tests_green" in require:
        (passed if _has(RE_TEST, text) else missing).append("tests_green")
    if "pushed_or_consolidated" in require:
        (passed if RE_PUSH.search(text) else missing).append("pushed_or_consolidated")

    # delivery_evidence (D-144): a delivery-only card must show a PUBLISHED
    # artifact URL. Without this the card inherited the code tier and was asked
    # for tests_green/ci_evidence/consolidated — structurally unsatisfiable for
    # a card whose whole job is "post the thing". gate_tick then blocked a `done`
    # card, the offload layer re-ran it, it finished `done` again, and the cycle
    # repeated every tick at priority 85, burning a worker session each pass.
    #
    # Deliberately scoped (see delivery_evidence_text) and FAIL CLOSED: the
    # aggregate blob leads with the card's own instructions, so reading it here
    # would credit the gate with the sentence "post the evidence to <PR URL>".
    # A caller that passes nothing therefore gets `missing`, never a pass.
    if "delivery_evidence" in require:
        (passed if delivery_evidence_present(delivery_text or "")
         else missing).append("delivery_evidence")

    ci = ci_result
    if "ci_evidence" in require and ci_required:
        ev = parse_ci_evidence(text)
        if not ev:
            missing.append("ci_evidence")
        elif ci is None:
            missing.append("ci_evidence")
        elif ci.get("verdict") == CI_GREEN:
            passed.append("ci_evidence")
        else:
            missing.append("ci_evidence")

    cross_family = None
    rev = spec.get("review") or {}
    if rev.get("required"):
        models = RE_MODEL.findall(text)
        author_fam = family(author_model, g)
        cf_ok = False
        for m in models:
            if family(m, g) not in (author_fam, "unknown") and author_fam != "unknown":
                cf_ok = True
                break
        if RE_APPROVE.search(text) and cf_ok:
            passed.append("cold_cross_family_review")
        else:
            missing.append("cold_cross_family_review")
        cross_family = cf_ok

    if "coverage" in require:
        thr = float(spec.get("coverage_threshold", 0) or 0)
        covs = [float(x) for x in RE_COVERAGE.findall(text)]
        (passed if covs and max(covs) >= thr else missing).append("coverage")

    # review_artifact (D-128 §20): filesystem/DB check, so only enforced when the
    # caller supplies the result (evaluate_task does; pure unit tests may omit it).
    if "review_artifact" in require and review_artifact is not None:
        (passed if review_artifact else missing).append("review_artifact")

    # review_published (completion integrity): a PR review must be POSTED, not
    # just drafted. Only enforced when the caller supplies the result — which
    # evaluate_task does only for tasks that reference a PR.
    if "review_published" in require and review_published is not None:
        (passed if review_published else missing).append("review_published")

    # consolidated (done == landed): the branch must be merged to the default
    # branch (our repos) or have exactly one open upstream PR (fork/third-party).
    if "consolidated" in require and consolidated is not None:
        (passed if consolidated else missing).append("consolidated")

    # review_benchmark_floor (N5): a review must be produced by a model meeting
    # its family tier's benchmark minima. Only enforced for review tasks.
    if "review_benchmark_floor" in require and review_benchmark_floor is not None:
        (passed if review_benchmark_floor else missing).append("review_benchmark_floor")

    # secrets_clean (§19.2): attestation line AND an independent clean re-scan.
    if "secrets_clean" in require and secrets_hits is not None:
        (passed if (RE_SECRET_CLEAN.search(text) and not secrets_hits)
         else missing).append("secrets_clean")

    # pr_branch_naming (D-135 §23): only evaluated when a PR is referenced
    # (caller passes None when the task has no PR).
    if "pr_branch_naming" in require and pr_branch is not None:
        (passed if pr_branch else missing).append("pr_branch_naming")

    # playbook_check (D-143): only evaluated when the card references a playbook
    # or fleet-role change (caller passes None otherwise), so unrelated code
    # work is never blocked.
    if "playbook_check" in require and playbook_check is not None:
        (passed if playbook_check else missing).append("playbook_check")

    # no_live_drift (Phase L / L4): authority=repo live files must equal
    # origin/master — no unmanaged live edits. Fail-open when the state file is
    # absent (auditor not yet run); blocks once the timer reports drift.
    if "no_live_drift" in require:
        paths = live_drift if live_drift is not None else live_drift_paths()
        (passed if not paths else missing).append("no_live_drift")

    enforce = spec.get("enforce", "advisory")
    verdict = "pass" if not missing else ("block" if enforce == "block" else "warn")
    return {"tier": tier, "verdict": verdict, "passed": passed,
            "missing": missing, "cross_family": cross_family,
            "author_family": family(author_model, g), "ci": ci,
            "secrets_clean": ("secrets_clean" not in missing) if "secrets_clean" in require
            and secrets_hits is not None else None,
            "secrets_hits": secrets_hits}


def evaluate_task(board: str, task_id: str, gates: dict | None = None) -> dict:
    db = BOARDS / board / "kanban.db"
    if not db.exists():
        return {"board": board, "id": task_id, "error": "no-board"}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select title, assignee, result, completed_at, created_at from tasks where id=?",
            (task_id,)).fetchone()
        if not row:
            return {"board": board, "id": task_id, "error": "no-task"}
        title, assignee, result, completed_at, created_at = row
        text = evidence_text(conn, task_id, result)
        tags = board_tags(conn, task_id, gates)
        # delivery_evidence is scoped to what the card CANNOT pre-fill: its own
        # RESULT plus comments written after completion (see
        # delivery_evidence_text). Never the aggregate blob above, which leads
        # with the card's own instructions.
        delivery = delivery_evidence_text(conn, task_id, result, completed_at)
    finally:
        conn.close()
    tier = classify_tier(board, tags, gates)
    author = profile_model(assignee or "")
    # Grandfather (D-132 addendum): exempt tasks whose effective time predates
    # the activation cutoff from ALL gates. effective = completed_at or created_at.
    since = enforce_since_ts(gates)
    effective = _to_epoch(completed_at) or _to_epoch(created_at)
    if since and effective and effective < since:
        return {"board": board, "id": task_id, "title": title, "assignee": assignee,
                "tier": tier, "verdict": "grandfathered", "passed": [], "missing": [],
                "cross_family": None, "author_family": family(author, gates),
                "ci_evidence": None, "ci": None, "ci_required": False,
                "grandfathered": True, "enforce_since_ts": since,
                "effective_ts": effective, "completed_at": completed_at,
                "created_at": created_at}
    # Per-task waiver (operator-approved, D-128 §operator discretion): the
    # spec's `task_waivers` map keys on "board:task" (or bare task) and carries
    # `reason`/`operator`. A listed task skips ALL code-tier gates — used only
    # where the completion evidence is genuinely done/merged/CI-green but some
    # mandatory gates are structurally unsatisfiable (e.g. ci_evidence tooling
    # reads ngit while the repo is GitHub-Actions-routed, or D-116 mandated a
    # non-pr/ branch that contradicts pr_branch_naming). This is a precise
    # per-task carve-out; the MANDATORY_CODE_GATES floor still holds for every
    # other card. Additions require an explicit operator decision.
    _g = gates if gates is not None else load_gates()
    _w = _g.get("task_waivers") or {}
    _waiver = _w.get(f"{board}:{task_id}") or _w.get(task_id)
    if _waiver:
        return {"board": board, "id": task_id, "title": title, "assignee": assignee,
                "tier": tier, "verdict": "waived", "passed": [], "missing": [],
                "cross_family": None, "author_family": family(author, gates),
                "ci_evidence": None, "ci": None, "ci_required": False,
                "waived": True, "waiver_reason": _waiver.get("reason"),
                "waiver_operator": _waiver.get("operator"),
                "completed_at": completed_at, "created_at": created_at}
    ev = parse_ci_evidence(text)
    ci = run_ci_evidence(ev, gates) if ev else None
    ra = review_artifact_present(board, task_id, text)
    # Completion integrity: only enforce a PUBLISHED review when the task
    # references a PR (no PR -> nothing to publish; the artifact gate still
    # applies). None means "not applicable" and is skipped in evaluate().
    rp = (review_published_present(board, task_id, text)
          if RE_PR_REF.search(text) else None)
    # consolidated: done == landed. Enforced for every code task.
    consolidated = consolidated_present(text)
    # review_benchmark_floor: only for review tasks (reviewer assignee, a
    # *review* board, or a reviewer_model line). Infer the family tier from the
    # model when the card omits `review_tier:`.
    is_review = (str(assignee or "").startswith(("worker-reviewer", "reviewer"))
                 or "review" in str(board).lower()
                 or bool(RE_REVIEWER_MODEL.search(text)))
    rbf: bool | None = None
    if is_review:
        _mm = RE_REVIEWER_MODEL.search(text)
        _tm = RE_REVIEW_TIER.search(text)
        _model = _mm.group(1) if _mm else ""
        _tier = _tm.group(1) if _tm else None
        if _tier is None and _model:
            _ml = _model.lower()
            for _fam in ("qwen", "glm", "kimi"):
                if _fam in _ml:
                    _tier = f"tier/review-{_fam}"
                    break
        rbf = review_benchmark_floor_ok(_model, _tier)
    secrets_hits = scan_secrets(text + "\n" + _attachment_text(board, task_id))
    # Only enforce the PR-branch rule when the task actually references a PR.
    # pr_branch_asserted() (not RE_PR_BRANCH.search) so that a QUOTED REFUSAL
    # cannot credit this floor gate (plebeian:t_7589d2c1).
    pr_branch = (pr_branch_asserted(text) if RE_PR_REF.search(text) else None)
    # playbook_check (D-143): only when the card references a playbook or a fleet
    # role (a change to roles/ or playbook*.yml). None means "not applicable" and
    # is skipped in evaluate().
    playbook_check = (playbook_check_present(text)
                      if RE_PLAYBOOK_REF.search(text) else None)
    res = evaluate(tier, text, author, gates, ci_result=ci, ci_required=True,
                   review_artifact=ra, review_published=rp,
                   consolidated=consolidated, review_benchmark_floor=rbf,
                   secrets_hits=secrets_hits,
                   pr_branch=pr_branch, playbook_check=playbook_check,
                   delivery_text=delivery)
    res.update({"board": board, "id": task_id, "title": title, "assignee": assignee,
                "ci_evidence": ev, "ci": ci, "ci_required": True,
                "review_artifact": ra, "review_published": rp,
                "consolidated": consolidated, "review_benchmark_floor": rbf,
                "secrets_hits": secrets_hits,
                "pr_branch_naming": pr_branch,
                "playbook_check": playbook_check,
                "completed_at": completed_at, "created_at": created_at,
                "enforce_since_ts": since})
    return res


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--board", required=True); p.add_argument("--task", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("classify")
    p.add_argument("--board", required=True); p.add_argument("--tags", default="")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("family")
    p.add_argument("--model", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "evaluate":
        r = evaluate_task(args.board, args.task)
        print(json.dumps(r, indent=1) if args.json
              else f"{r.get('board')}/{r.get('id')}: tier={r.get('tier')} "
                   f"verdict={r.get('verdict')} missing={r.get('missing')}")
        return 0
    if args.cmd == "classify":
        t = classify_tier(args.board, [x for x in args.tags.split(",") if x])
        print(json.dumps({"board": args.board, "tier": t}) if args.json else t)
        return 0
    if args.cmd == "family":
        print(family(args.model))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

