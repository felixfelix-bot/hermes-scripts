#!/usr/bin/env python3
"""gate_engine.py — D-128 tiered quality-gate evaluation (pure + CLI).

Classifies a task into a tier (code | docs), then evaluates the completion
evidence against the tier's required gates:

  tests_green            a passing test/smoke run is recorded
  ci_evidence            a live CI result is cited (workflow + conclusion), or a
                         documented absence is on record
  cold_cross_family_review  an APPROVED review by a *different* model family
  pushed_or_consolidated a push / PR / consolidation branch is recorded
  coverage               optional coverage % >= threshold

`code` tier is enforced (block); `docs` is advisory. Unknown ⇒ code (fail safe).

ci_evidence semantics (D-128 extension, 2026-09-13)
---------------------------------------------------
Recognised forms in the task evidence text ([result] + task comments):

1. **CI citation** — one line that carries *all* of
     * the token ``ngit CI``,
     * a workflow path ending in ``.yml`` / ``.yaml``, and
     * a conclusion word (``success`` / ``failure`` / ``timed_out`` /
       ``startup_failure`` / ``cancelled``),
   e.g. ``ngit CI .github/workflows/ci.yml -> success``;
   **or** the structured marker ``ci_evidence: workflow=<path> conclusion=<word>``
   (the conclusion must be a recognised conclusion word).

2. **Documented absence** — ``no CI evidence available (reason: ...)``.

Documented-absence decision: an absence is *recognised* (so it lands on the
record and ``ci_absence_documented`` is set in the result) but it is **never a
pass**. It stays in ``missing`` at every tier, which means it cannot satisfy the
``code`` (code/risky) tier. The ``docs`` tier does not require this gate at all
(docs/light changes frequently have no CI run), so there is no tier where a
documented absence counts as a pass.

Deliberately NOT accepted: the bare words ``CI`` / ``ngit`` on their own. The
citation must pair a workflow path with a conclusion word, so a handoff that
merely mentions "CI" or "CI green" does not satisfy the gate.

The gate proves CI evidence was *obtained and cited*; it does not itself judge
greenness — a cited ``failure`` still counts as a citation. Whether the head is
green is decided by the review, whose verdict must cite per-workflow
conclusions (see the sdlc-review skill).

Spec resolution / fail-closed (2026-09-13)
------------------------------------------
load_gates() tries, in order: ``$HERMES_HOME/bot/gates.json``, then
``<root>/bot/gates.json`` (~/.hermes/bot/gates.json — the real spec here;
``<root>`` is resolved the way hermes_constants.get_default_hermes_root()
does, so a profile HERMES_HOME does not hide it), then ``gates.json`` next to
this module, then the checked-in ``gates.default.json`` next to this module.
A candidate only counts when it parses to a JSON object with a non-empty
``tiers`` map. If none do (or the tier is absent from the spec), evaluate()
returns a hard BLOCK with ``missing=["gate_spec"]`` and the paths that were
tried — a missing spec is NEVER a pass.

Pure functions are unit-tested (tests/test_gate_engine.py). CLI:
  gate_engine.py evaluate --board B --task T [--json]
  gate_engine.py classify --board B [--tags a,b] [--json]
  gate_engine.py family --model glm-5.3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
MODULE_DIR = Path(__file__).resolve().parent


def _agent_root(home: Path) -> Path:
    """Resolve the shared agent root.

    Mirrors ``hermes_constants.get_default_hermes_root()``: ``HERMES_HOME`` is
    frequently a *profile* dir (``<root>/profiles/<name>``) — which is what
    every cron tick in this profile gets — while the shared dirs
    (``profiles/``, ``bot/``, ``kanban/``) live at ``<root>``. Resolving them
    straight off the profile home silently finds nothing, which is the
    "green because nothing ran" failure this engine must never reproduce.
    """
    native = Path(os.path.expanduser("~/.hermes"))
    try:
        home.resolve().relative_to(native.resolve())
        return native
    except ValueError:
        pass
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


ROOT = _agent_root(HERMES)
# NOTE: BOARDS stays HERMES-anchored (deliberately NOT changed here).
# hermes_cli resolves boards through get_default_hermes_root()
# (~/.hermes/kanban/boards), so when HERMES_HOME is a profile dir this path
# points at a stale copy of the board tree. Switching it live is a fleet-wide
# decision, not a bug-fix: with the spec now loading, a tick over the real
# root blocks every done card of the last 24h (measured 2026-09-13:
# 199 of 199 cards lacked ci_evidence + cold_cross_family_review).
BOARDS = HERMES / "kanban" / "boards"
PROFILES = ROOT / "profiles"
GATES = HERMES / "bot" / "gates.json"              # $HERMES_HOME/bot/gates.json
SHARED_GATES = ROOT / "bot" / "gates.json"         # <root>/bot/gates.json (~/.hermes)
LOCAL_GATES = MODULE_DIR / "gates.json"            # sits next to this module
DEFAULT_GATES = MODULE_DIR / "gates.default.json"  # checked-in fallback

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

# ci_evidence (D-128 extension) — see the ci_evidence semantics note up top.
# A citation must carry the `ngit CI` token, a workflow .yml/.yaml path AND a
# conclusion word on the SAME line — the bare word "CI" never satisfies it.
_CI_CONCLUSIONS = r"success|failure|timed_out|startup_failure|cancelled|canceled"
RE_CI_CITE = re.compile(r"\bngit\s+ci\b", re.I)
RE_CI_WORKFLOW = re.compile(r"[\w./-]+\.ya?ml\b", re.I)
RE_CI_CONCLUSION = re.compile(rf"\b(?:{_CI_CONCLUSIONS})\b", re.I)
RE_CI_MARKER = re.compile(
    r"ci_evidence\s*[:=]\s*workflow\s*=\s*(\S+)\s+conclusion\s*=\s*([A-Za-z_]+)", re.I)
RE_CI_ABSENCE = re.compile(
    r"\bno\s+ci\s+evidence\s+available\b\s*(?:\(reason\s*:[^)]*\))?", re.I)


def _read(p, d=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def gate_spec_paths(path: Path | str | None = None) -> list[Path]:
    """Ordered candidate locations for the gate spec.

    Resolution order: ``$HERMES_HOME/bot/gates.json`` (may be a *profile*
    dir), then ``<root>/bot/gates.json`` (~/.hermes/bot/gates.json — the real
    spec on this fleet), then ``gates.json`` next to this module, and finally
    the checked-in ``gates.default.json`` next to this module.
    """
    if path is not None:
        return [Path(path)]
    cands: list[Path] = []
    for c in (GATES, SHARED_GATES, LOCAL_GATES, DEFAULT_GATES):
        if c not in cands:
            cands.append(c)
    return cands


def load_gates(path: Path | str | None = None) -> dict:
    """Return the first *usable* gate spec, else a fail-closed error spec.

    "Usable" means a JSON object carrying a non-empty ``tiers`` map: an
    unparseable or tier-less spec must never be read as "no gates required"
    (that is how the engine used to report PASS with nothing enforced). On
    total failure the returned dict carries ``_spec_error`` / ``_spec_tried``,
    which :func:`evaluate` turns into a hard ``block``.
    """
    tried: list[str] = []
    for cand in gate_spec_paths(path):
        tried.append(str(cand))
        spec = _read(cand)
        if isinstance(spec, dict) and spec.get("tiers"):
            spec = dict(spec)
            spec["_spec_path"] = str(cand)
            return spec
    return {
        "_spec_error": "no usable gate spec: need a JSON object with a "
                       "non-empty 'tiers' map",
        "_spec_tried": tried,
    }


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


def classify_tier(board: str, tags: list[str] | None = None,
                  gates: dict | None = None) -> str:
    g = gates or load_gates()
    if board in (g.get("board_tiers") or {}):
        return g["board_tiers"][board]
    for t in (tags or []):
        t = t.strip().lower()
        if t in (g.get("tag_tiers") or {}):
            return g["tag_tiers"][t]
    return g.get("default_tier", "code")


def board_tags(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Best-effort tag extraction from the task body/result (light heuristic)."""
    try:
        row = conn.execute(
            "select coalesce(body,'')||' '||coalesce(result,'') from tasks where id=?",
            (task_id,)).fetchone()
    except sqlite3.Error:
        return []
    text = (row[0] if row else "") or ""
    m = re.search(r"tags?\s*[:=]\s*([A-Za-z0-9_,\- ]+)", text, re.I)
    return [t.strip() for t in m.group(1).split(",")] if m else []


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


def ci_evidence(text: str) -> dict:
    """Recognise a CI-evidence citation or a documented absence.

    Returns ``{"cited": bool, "absence": bool, "line": str}``.

    ``cited`` is True only for a real citation: a single line carrying the
    ``ngit CI`` token, a workflow ``.yml``/``.yaml`` path and a conclusion word,
    or a ``ci_evidence: workflow=... conclusion=...`` marker with a recognised
    conclusion. A documented absence (``no CI evidence available (reason: ...)``)
    sets ``absence`` but never ``cited`` — see the module docstring for why an
    absence is never a pass.
    """
    lines = (text or "").splitlines()
    for line in lines:
        m = RE_CI_MARKER.search(line)
        if m and RE_CI_CONCLUSION.fullmatch(m.group(2).strip()):
            return {"cited": True, "absence": False, "line": line.strip()}
        if (RE_CI_CITE.search(line) and RE_CI_WORKFLOW.search(line)
                and RE_CI_CONCLUSION.search(line)):
            return {"cited": True, "absence": False, "line": line.strip()}
    for line in lines:
        if RE_CI_ABSENCE.search(line):
            return {"cited": False, "absence": True, "line": line.strip()}
    return {"cited": False, "absence": False, "line": ""}


def _fail_closed(tier: str, author_model: str, g: dict) -> dict:
    """Hard block used when the spec (or the tier inside it) cannot be read.

    Never a pass: a missing/unparseable spec means *nothing was enforced*, so
    the only honest verdict is a block that names the paths that were tried.
    """
    return {
        "tier": tier, "verdict": "block", "passed": [],
        "missing": ["gate_spec"], "cross_family": None,
        "ci_absence_documented": None,
        "author_family": family(author_model, g),
        "spec_path": g.get("_spec_path"),
        "spec_error": g.get("_spec_error")
        or f"tier {tier!r} is not defined in spec {g.get('_spec_path')}",
        "spec_tried": g.get("_spec_tried") or [str(g.get("_spec_path"))],
    }


def evaluate(tier: str, text: str, author_model: str,
             gates: dict | None = None) -> dict:
    """Return {tier, verdict, passed, missing, cross_family, spec_path}.

    FAIL CLOSED: if no gate spec parsed (or the spec has no entry for
    ``tier``) the verdict is ``block`` with ``missing=['gate_spec']`` — the
    engine never reports ``pass`` with nothing enforced.
    """
    g = gates or load_gates()
    spec = (g.get("tiers") or {}).get(tier)
    if g.get("_spec_error") or not isinstance(spec, dict):
        return _fail_closed(tier, author_model, g)
    require = spec.get("require", [])

    passed, missing = [], []
    if "tests_green" in require:
        (passed if _has(RE_TEST, text) else missing).append("tests_green")
    if "pushed_or_consolidated" in require:
        (passed if RE_PUSH.search(text) else missing).append("pushed_or_consolidated")

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

    # ci_evidence (D-128 extension): only a real citation passes; a documented
    # absence is recognised (ci_absence_documented) but stays in `missing`.
    ci_absent = None
    if "ci_evidence" in require:
        ci = ci_evidence(text)
        ci_absent = ci["absence"]
        (passed if ci["cited"] else missing).append("ci_evidence")

    enforce = spec.get("enforce", "advisory")
    verdict = "pass" if not missing else ("block" if enforce == "block" else "warn")
    return {"tier": tier, "verdict": verdict, "passed": passed,
            "missing": missing, "cross_family": cross_family,
            "ci_absence_documented": ci_absent,
            "author_family": family(author_model, g),
            "spec_path": g.get("_spec_path"), "spec_error": None}


def evaluate_task(board: str, task_id: str, gates: dict | None = None) -> dict:
    db = BOARDS / board / "kanban.db"
    if not db.exists():
        return {"board": board, "id": task_id, "error": "no-board"}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select title, assignee, result from tasks where id=?", (task_id,)).fetchone()
        if not row:
            return {"board": board, "id": task_id, "error": "no-task"}
        title, assignee, result = row
        text = evidence_text(conn, task_id, result)
        tags = board_tags(conn, task_id)
    finally:
        conn.close()
    tier = classify_tier(board, tags, gates)
    res = evaluate(tier, text, profile_model(assignee or ""), gates)
    res.update({"board": board, "id": task_id, "title": title, "assignee": assignee})
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
        if args.json:
            print(json.dumps(r, indent=1))
        else:
            print(f"{r.get('board')}/{r.get('id')}: tier={r.get('tier')} "
                  f"verdict={r.get('verdict')} missing={r.get('missing')} "
                  f"spec={r.get('spec_path')}")
            if r.get("spec_error"):
                print(f"gate_engine: SPEC ERROR — {r['spec_error']}; "
                      f"tried={r.get('spec_tried')}")
        return 2 if r.get("spec_error") else 0
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
