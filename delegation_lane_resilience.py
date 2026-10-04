#!/usr/bin/env python3
"""delegation_lane_resilience.py — THE delegation-policy applier (single source).

This is the ONE canonical applier for the manager profile's delegation
config-as-code. The policy lives in ``state/fleet/delegation_policy.json``
(primary lane pin + cross-family fallback chain); this tool converges and
checks it. The module keeps its historical name (referenced by roles, tests
and docs — renaming adds churn for no gain); this docstring carries the truth.

WHY (measured 2026-09-29, kanban t_147fbb26)
--------------------------------------------
``delegate_task`` pins EVERY child on this node to one lane: children are built
with ``model=delegation.model`` / ``provider=delegation.provider``
(``tools/delegate_tool.py::_resolve_delegation_credentials``) and there is no
per-task model parameter. So a single transient burst on that lane has
fleet-wide blast radius for delegated work. Attempt ``deleg_dfc43c40`` died
mid-run on

    HTTP 503 {"error": "all providers exhausted (flat router)",
              "capacity_exhausted": true, "model": "<the pinned lane>",
              "candidates_tried": ["ours", "ollama_cloud_2", "friend"]}

after 3 retries and produced nothing at all — no branch on origin, no local
branch, no worktree. Minutes later the same model answered HTTP 200 three times
in a row and ``dispatch_gate`` still reported ``can_dispatch: true``: a lane
BURST, not an outage.

The cure is not a better single pin. ``delegate_tool`` already hands the parent's
fallback chain to every child (``child fallback_model=parent_agent._fallback_chain``),
so a chain in the profile's ``fallback_providers`` turns a lane burst into a
failover instead of a lost task — provided the chain's early entries are OTHER
families than the profile's own, so a delegated *review* that fails over is still
a cross-family review (AGENTS.md D-128 §4).

The desired state is version-controlled in
``state/fleet/delegation_policy.json``; this tool converges and checks it:

    delegation_lane_resilience.py --check [--json]   # exit 1 = drift/trap
    delegation_lane_resilience.py --apply [--json]   # idempotent, backs up first
    delegation_lane_resilience.py --probe            # do the lanes actually answer?
    delegation_lane_resilience.py --check --file state/manager/config.sanitized.yaml

``--check --file`` exists on purpose: the committed live->repo mirror
(``state/manager/config.sanitized.yaml``, written by ``scripts/sync/state_sync.py``)
is what a reviewer reads as the live pin, so the same invariant must hold for
both documents. ``--apply`` only ever rewrites the ``delegation`` block's own
keys plus the ``fallback_providers`` block, so unrelated live edits survive
byte-for-byte.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLICY_NAME = "delegation_policy.json"
HOME = Path(os.path.expanduser("~"))

# Built-in fallback family table. The authoritative one is
# scripts/governance/gates.default.json -> families; it win when readable, so a
# new vendor family reaches this guard through the repo, not a code edit.
BUILTIN_FAMILIES = {
    "glm": "zhipu", "kimi": "moonshot", "qwen": "alibaba", "deepseek": "deepseek",
    "gpt": "openai", "o1": "openai", "o3": "openai", "claude": "anthropic",
    "gemini": "google", "llama": "meta", "mistral": "mistral", "grok": "xai",
    "tencent": "tencent", "hy4": "tencent", "hunyuan": "tencent",
}


# --------------------------------------------------------------------------- #
# locations / policy
# --------------------------------------------------------------------------- #
def repo_root(start: Path | None = None) -> Path:
    """Locate the orchestration repo root (dir containing state/fleet/)."""
    start = start or Path(__file__).resolve()
    for p in [start, *start.parents]:
        if (p / "state" / "fleet").is_dir():
            return p
    return Path(__file__).resolve().parents[2]


def hermes_root() -> Path:
    """Hermes data root, tolerating HERMES_HOME pointing at a profile dir.

    Cron sets HERMES_HOME to ``~/.hermes/profiles/manager``, from which
    ``<root>/profiles`` would be ``~/.hermes/profiles/profiles`` — the
    'guard never fires' layout bug.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        p = Path(env).expanduser()
        if "profiles" in p.parts:
            i = p.parts.index("profiles")
            return Path(*p.parts[:i]) if i else Path(os.sep)
        return p
    return HOME / ".hermes"


def asset(name: str) -> Path:
    """Policy file: repo copy first, deployed copy second."""
    for cand in (repo_root() / "state" / "fleet" / name,
                 hermes_root() / "bot" / name,
                 HERE / name):
        if Path(cand).is_file():
            return Path(cand)
    raise FileNotFoundError(f"{name} not found (repo state/fleet or bot dir)")


def families() -> dict:
    """Family prefix map, from the repo's shipped gate spec when readable."""
    for cand in (repo_root() / "scripts" / "governance" / "gates.default.json",
                 hermes_root() / "bot" / "gates.json"):
        try:
            fam = json.loads(Path(cand).read_text()).get("families")
        except (OSError, ValueError):
            continue
        if isinstance(fam, dict) and fam:
            return {str(k): str(v) for k, v in fam.items()}
    return dict(BUILTIN_FAMILIES)


def model_family(model: str, fam: dict) -> str:
    """Vendor family of a model id, '' when unresolvable.

    Empty means 'cannot tell' — callers must never treat it as a match, so a
    vacuous ``unknown == unknown`` can't turn the guard green.
    """
    mid = (model or "").strip().lower().split("/")[-1]
    if not mid:
        return ""
    # longest prefix wins (a v4-flash id -> deepseek, ``o1-mini`` -> o1)
    for prefix in sorted(fam, key=len, reverse=True):
        if mid == prefix or mid.startswith(prefix + "-") or mid.startswith(prefix + "."):
            return fam[prefix]
    return ""


# --------------------------------------------------------------------------- #
# minimal line-targeted YAML block reader/writer
# --------------------------------------------------------------------------- #
class BlockError(ValueError):
    """The block exists but is not a plain top-level mapping/sequence we can edit."""


def _split_comment(raw: str) -> tuple[str, str]:
    """('value', ' # comment') — honours quotes when finding the comment."""
    out: list[str] = []
    quote, i = "", 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            if ch == quote:
                quote = ""
            out.append(ch)
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or raw[i - 1].isspace()):
            return "".join(out), raw[i:]
        else:
            out.append(ch)
        i += 1
    return "".join(out), ""


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def block_span(text: str, block: str) -> dict | None:
    """Locate a top-level ``block:`` node.

    Returns {"start","end","indent","inline"} where ``inline`` carries any
    non-comment value on the header line (``block: []`` / ``block: {...}``).
    ``None`` when the block is absent.
    """
    lines = (text or "").splitlines()
    header = re.compile(rf"^{re.escape(block)}\s*:\s*(.*)$")
    for i, ln in enumerate(lines):
        m = header.match(ln)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest and not rest.startswith("#"):
            return {"start": i, "end": i + 1, "indent": 2, "inline": rest}
        end = len(lines)
        for j in range(i + 1, len(lines)):
            body = lines[j]
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            if len(body) - len(body.lstrip()) == 0:
                end = j
                break
        return {"start": i, "end": end, "indent": 2, "inline": ""}
    return None


def parse_yaml_block(text: str, block: str) -> dict:
    """Top-level ``block:`` mapping as {key: scalar}; keys at child indent only.

    A nested mapping (deeper indent, e.g. ``delegation.extra_body.model``) is
    never mistaken for the block's own key. Raises BlockError for a flow-style
    header (that shape is not editable line-wise and must fail loudly).
    """
    span = block_span(text, block)
    if span is None:
        return {}
    if span["inline"] and span["inline"] != "{}":
        raise BlockError(f"'{block}:' uses flow style on the header line; refusing to guess")
    out: dict[str, str] = {}
    lines = (text or "").splitlines()
    for ln in lines[span["start"] + 1:span["end"]]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) != span["indent"]:
            continue
        m = re.match(r"^\s+([A-Za-z_][\w.-]*)\s*:\s*(.*)$", ln)
        if not m:
            continue
        raw, _ = _split_comment(m.group(2))
        out[m.group(1)] = _unquote(raw.strip())
    return out


_YAML_AMBIGUOUS = {"no", "yes", "true", "false", "on", "off", "null", "~",
                   "none", "None", "True", "False", "Null"}
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _yaml_scalar(value: str) -> str:
    v = str(value)
    if v in _YAML_AMBIGUOUS or _NUMERIC.match(v):
        return "'" + v.replace("'", "''") + "'"
    if re.fullmatch(r"[A-Za-z0-9._:/+-]+", v):
        return v
    return "'" + v.replace("'", "''") + "'"


def set_block_key(text: str, block: str, key: str, value: str) -> tuple[str, bool]:
    """Set ``block.key`` at the block's OWN child indent. Returns (text, changed).

    Every other line is preserved byte-for-byte, including a trailing inline
    comment on the very key being set.
    """
    span = block_span(text, block)
    if span is not None and span["inline"]:
        raise BlockError(f"'{block}:' uses flow style on the header line; refusing to edit")
    lines = text.splitlines(keepends=True)
    val = _yaml_scalar(value)
    if span is None:
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        return f"{text}{prefix}{block}:\n  {key}: {val}\n", True
    start, end = span["start"], span["end"]
    key_re = re.compile(rf"^(\s+)({re.escape(key)})\s*:\s*(.*?)(\s*)$")
    for j in range(start + 1, end):
        raw_line = lines[j].rstrip("\n")
        if len(raw_line) - len(raw_line.lstrip()) != 2:
            continue
        m = key_re.match(raw_line)
        if not m:
            continue
        body_region, comment = _split_comment(m.group(3))
        body = body_region.rstrip()
        gap = body_region[len(body):]
        if _unquote(body.strip()) == value:
            return text, False
        lines[j] = f"  {key}: {val}{gap}{comment}\n"
        return "".join(lines), True
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"  {key}: {val}\n")
    return "".join(lines), True


def render_list_block(block: str, entries: list[dict], order: list[str]) -> str:
    """Render a top-level sequence-of-mappings block (stable key order)."""
    out = [f"{block}:\n"]
    for e in entries:
        first = True
        for k in order:
            if k not in e:
                continue
            prefix = "  - " if first else "    "
            out.append(f"{prefix}{k}: {_yaml_scalar(e[k])}\n")
            first = False
        if first:                      # entry had none of `order`
            raise BlockError(f"{block}: entry {e!r} has no rendered keys")
    return "".join(out)


def set_list_block(text: str, block: str, entries: list[dict],
                   order: list[str]) -> tuple[str, bool]:
    """Replace/insert ``block:`` as a sequence of mappings. Returns (text, changed).

    Accepts an existing block, an inline empty sequence (``block: []``), or
    absence (append at end of document). Refuses a non-empty flow sequence.
    """
    rendered = render_list_block(block, entries, order)
    lines = text.splitlines(keepends=True)
    span = block_span(text, block)
    if span is None:
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        return f"{text}{prefix}{rendered}", True
    if span["inline"]:
        if span["inline"] not in ("[]", "[ ]"):
            raise BlockError(f"'{block}:' is a non-empty flow sequence; refusing to rewrite it")
        lines[span["start"]] = rendered
        return "".join(lines), True
    start, end = span["start"], span["end"]
    body = "".join(lines[start + 1:end])
    if body == rendered.split("\n", 1)[1]:          # byte-identical body+keys
        return text, False
    lines[start:end] = [rendered]
    return "".join(lines), True


def duplicate_block_keys(text: str, block: str) -> list[str]:
    """Keys appearing more than once at ``block``'s child indent (a corrupt write)."""
    span = block_span(text, block)
    if span is None or span["inline"]:
        return []
    seen: set[str] = set()
    dupes: list[str] = []
    for ln in text.splitlines()[span["start"] + 1:span["end"]]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) != 2:
            continue
        m = re.match(r"^\s+([A-Za-z_][\w.-]*)\s*:", ln)
        if m:
            if m.group(1) in seen:
                dupes.append(m.group(1))
            seen.add(m.group(1))
    return dupes


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def evaluate_text(text: str, spec: dict, fam: dict) -> dict:
    """Check ONE profile config document against ONE policy entry (pure)."""
    problems: list[str] = []
    entry: dict = {"expected_model": (spec.get("primary") or {}).get("model", "")}
    try:
        deleg = parse_yaml_block(text, "delegation")
    except BlockError as exc:
        entry.update({"status": "unparsable", "problems": [str(exc)]})
        entry["problems"] = [str(exc)]
        return entry

    own_model = ""
    for block in ("model",):
        try:
            own_model = str(parse_yaml_block(text, block).get("default", "") or "")
        except BlockError:
            own_model = ""
    author_family = spec.get("author_family") or model_family(own_model, fam)
    entry.update({"own_model": own_model, "author_family": author_family,
                  "delegation_model": deleg.get("model", "")})

    primary = spec.get("primary") or {}
    for key in ("model", "provider", "base_url"):
        want = str(primary.get(key, "") or "")
        if want and str(deleg.get(key, "") or "") != want:
            problems.append(
                f"delegation.{key} is '{deleg.get(key, '')}', policy primary wants '{want}'")

    pin_model = str(deleg.get("model", "") or "")
    pin_family = model_family(pin_model, fam)
    entry["delegation_family"] = pin_family
    if pin_model and pin_family and author_family and pin_family == author_family:
        problems.append(
            f"delegation lane family '{pin_family}' == the profile's own family "
            f"'{author_family}' ({pin_model}); every child runs in the author's family "
            "(D-128 §4 — see state/fleet/delegation_family_policy.json)")

    # ---- the resilience chain ------------------------------------------- #
    want_chain = list(spec.get("fallback_providers") or [])
    order = ["model", "provider", "base_url"]
    try:
        got_chain = parse_yaml_sequence(text, "fallback_providers", order)
    except BlockError as exc:
        problems.append(str(exc))
        got_chain = None
    entry["fallback_chain"] = got_chain if got_chain is not None else "unparsable"
    if got_chain is not None:
        n = len(got_chain)
        if n < len(want_chain):
            problems.append(
                f"fallback_providers has {n} entr{'y' if n == 1 else 'ies'}, policy wants "
                f"{len(want_chain)} — a single pinned lane is the single point of failure "
                "this card is about (t_147fbb26)")
        for i, want in enumerate(want_chain):
            if i >= n:
                break
            for key in order:
                w = str(want.get(key, "") or "")
                if w and str(got_chain[i].get(key, "") or "") != w:
                    problems.append(
                        f"fallback_providers[{i}].{key} is "
                        f"'{got_chain[i].get(key, '')}', policy wants '{w}'")
        # family shape: N distinct non-author families before any same-family entry
        seen: list[str] = []
        same_family_at: list[int] = []
        unknown_at: list[int] = []
        for i, e in enumerate(got_chain):
            f = model_family(str(e.get("model", "")), fam)
            if not f:
                unknown_at.append(i)
                continue
            if f == author_family:
                same_family_at.append(i)
            elif f not in seen:
                seen.append(f)
        if unknown_at:
            problems.append(
                f"fallback_providers entries {unknown_at} have an unresolvable family — "
                "the chain cannot be proven cross-family, so it fails closed")
        need = int(spec.get("min_distinct_non_author_families", 0) or 0)
        if len(seen) < need:
            problems.append(
                f"fallback chain covers {len(seen)} distinct non-author families "
                f"{seen}, policy requires {need}")
        if spec.get("require_cross_family_first") and same_family_at:
            last = len(got_chain) - 1
            if any(i != last for i in same_family_at):
                problems.append(
                    f"fallback_providers[{same_family_at}] share the primary/author family "
                    f"'{author_family}' but are not last — a review that fails over there is "
                    "no longer cross-family (D-128 §4)")

    entry.update({"problems": problems, "status": "ok" if not problems else "drift"})
    return entry


def parse_yaml_sequence(text: str, block: str, keys: list[str]) -> list[dict]:
    """Top-level ``block:`` sequence of mappings as [ {key: scalar} ]. Raises BlockError."""
    span = block_span(text, block)
    if span is None:
        return []
    if span["inline"] and span["inline"] not in ("[]", "[ ]"):
        raise BlockError(f"'{block}:' is an inline flow sequence; refusing to guess")
    if span["inline"]:
        return []
    lines = (text or "").splitlines()
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines[span["start"] + 1:span["end"]]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        m = re.match(r"^\s*-\s*([A-Za-z_][\w.-]*)\s*:\s*(.*)$", ln)
        if ind == 2 and m:
            cur = {}
            raw, _ = _split_comment(m.group(2))
            cur[m.group(1)] = _unquote(raw.strip())
            out.append(cur)
            continue
        m2 = re.match(r"^\s+([A-Za-z_][\w.-]*)\s*:\s*(.*)$", ln)
        if ind == 4 and m2 and cur is not None:
            raw, _ = _split_comment(m2.group(2))
            cur[m2.group(1)] = _unquote(raw.strip())
            continue
    return out


def evaluate(root: Path, policy: dict, file_override: str = "") -> dict:
    fam = families()
    report: dict = {"root": str(root), "profiles": {}, "problems": []}
    for name, spec in (policy.get("profiles") or {}).items():
        cfg = Path(file_override) if file_override else root / "profiles" / name / "config.yaml"
        entry: dict = {"profile": name, "config": str(cfg)}
        if not cfg.is_file():
            entry["status"] = "missing_config"
            report["profiles"][name] = entry
            continue
        res = evaluate_text(cfg.read_text(), spec, fam)
        entry.update(res)
        report["profiles"][name] = entry
        report["problems"].extend(f"{name}: {p}" for p in res.get("problems") or [])
    return report


def probe_lane(model: str, base_url: str, timeout: float = 30.0) -> dict:
    """One tiny completion; returns {requested, served, ok, detail}."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = json.dumps({"model": model, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "Reply OK."}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001 — surfaced, never raised
        return {"requested": model, "served": "", "ok": False, "detail": str(exc)[:200]}
    served = str(data.get("model") or "")
    return {"requested": model, "served": served, "ok": bool(served),
            "detail": "" if served else json.dumps(data)[:200]}


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def render_for_spec(text: str, spec: dict) -> str:
    """Return the converged text for ONE document (raises BlockError on refusal)."""
    primary = spec.get("primary") or {}
    out = text
    for key in ("model", "provider", "base_url"):
        want = str(primary.get(key, "") or "")
        if want:
            out, _ = set_block_key(out, "delegation", key, want)
    chain = []
    for e in spec.get("fallback_providers") or []:
        chain.append({k: e[k] for k in ("model", "provider", "base_url") if e.get(k)})
    if chain:
        out, _ = set_list_block(out, "fallback_providers", chain,
                                ["model", "provider", "base_url"])
    return out


def _write_config(cfg: Path, new_text: str, label: str) -> Path:
    backup_dir = HOME / "reports" / label / "tmp"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"config.yaml.bak-{int(time.time())}-{random.randint(0, 9999):04d}"
    shutil.copy2(cfg, backup)
    cfg.write_text(new_text)
    return backup


def apply_policy(root: Path, policy: dict, label: str) -> dict:
    """Converge every in-scope profile. Idempotent; abandons a corrupting write."""
    out: dict = {"changed": [], "unchanged": [], "backups": {}, "problems": []}
    for name, spec in (policy.get("profiles") or {}).items():
        cfg = root / "profiles" / name / "config.yaml"
        if not cfg.is_file():
            out["problems"].append(f"{name}: no config at {cfg}")
            continue
        original = cfg.read_text()
        try:
            text = render_for_spec(original, spec)
        except BlockError as exc:
            out["problems"].append(f"{name}: {exc}")
            continue
        if text == original:
            out["unchanged"].append(name)
            continue
        dupes = duplicate_block_keys(text, "delegation")
        if dupes:
            out["problems"].append(
                f"{name}: refusing to write — edit would duplicate key(s) {sorted(set(dupes))}")
            continue
        # the write must re-read as the policy wants, or it is not written at all
        check = evaluate_text(text, spec, families())
        if check.get("problems"):
            out["problems"].append(
                f"{name}: refusing to write — post-edit self-check still reports "
                f"{check['problems']}")
            continue
        out["backups"][name] = str(_write_config(cfg, text, label))
        out["changed"].append(name)
    out["report"] = evaluate(root, policy)
    out["problems"] = out["problems"] + list(out["report"]["problems"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "delegation lane resilience").splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 on drift/trap (default)")
    ap.add_argument("--apply", action="store_true", help="rewrite the live config to the policy")
    ap.add_argument("--probe", action="store_true", help="probe the pinned lanes through the router")
    ap.add_argument("--policy", default="", help="override the policy file path")
    ap.add_argument("--file", default="",
                    help="check THIS document instead of the live profile config "
                         "(e.g. --file state/manager/config.sanitized.yaml)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--label", default="delegation-lane-resilience",
                    help="backup dir under ~/reports")
    args = ap.parse_args(argv)
    if args.apply and args.probe:
        print("delegation-lane-resilience: --apply and --probe are mutually exclusive",
              file=sys.stderr)
        return 2

    policy = json.loads(Path(args.policy).read_text() if args.policy else asset(POLICY_NAME).read_text())
    root = hermes_root()

    if args.apply:
        result = apply_policy(root, policy, args.label)
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            for name in result["changed"]:
                print(f"delegation-lane-resilience: applied {name} "
                      f"(backup {result['backups'][name]})")
            for name in result["unchanged"]:
                print(f"delegation-lane-resilience: {name} already converged")
            for p in result["problems"]:
                print(f"  PROBLEM {p}")
        return 1 if result["problems"] else 0

    file_override = str((repo_root() / args.file) if args.file and not os.path.isabs(args.file)
                        else args.file)
    report = evaluate(root, policy, file_override)
    if args.probe:
        report["probe"] = {}
        for name, entry in report["profiles"].items():
            spec = (policy.get("profiles") or {}).get(name) or {}
            lanes = [spec.get("primary") or {}] + list(spec.get("fallback_providers") or [])
            report["probe"][name] = [
                probe_lane(str(l.get("model", "")),
                           str(l.get("base_url") or "http://localhost:9099"))
                for l in lanes if l.get("model")]
    if args.json:
        print(json.dumps(report, indent=1))
    elif report["problems"]:
        print("delegation-lane-resilience: DRIFT")
        for p in report["problems"]:
            print(f"  - {p}")
    else:
        chain = {n: " -> ".join(str(e.get("model", "")) for e in
                                (report["profiles"][n].get("fallback_chain") or []))
                 for n in report["profiles"]}
        print(f"delegation-lane-resilience: OK (fallback chain {chain})")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
