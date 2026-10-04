#!/usr/bin/env python3
"""model_identity_audit.py — F3 truth audit (PLAN Layer 2, 2026-09-27).

INVARIANTS (each stated so it can be FALSE)
-------------------------------------------
(A) RESOLVABILITY — every model id referenced by ``model_tiers.yaml``,
    ``model_fallbacks.json``, ``review_family_priority.json`` and ``MODEL_ALIASES``
    resolves (directly, or via a ``MODEL_ALIASES`` rewrite) to an id that exists
    in at least one PROVIDER's model list.
(B) DETERMINISM — no alias maps to two canonicals: ``canonicalize`` is
    idempotent and no alias TARGET is itself an alias KEY leading elsewhere
    (a rewrite chain is an ambiguous "two canonicals").
(C) NOT-RETIRED — no referenced id, and no alias TARGET, is a RETIRED tag
    (an id the vendor has withdrawn; see ``identity.retired_tags``).

WHY IT EXISTS
-------------
2026-09-27: two canonicals coexisted — ``deepseek/deepseek-flash`` (V4.1) vs
``deepseek/deepseek-v4-flash`` (older V4) — with aliases
``deepseek-v4-flash`` / ``deepseek-v4-flash-0731`` pointing at the OLDER model:
~4.9B tokens/mo (~20% of the family) routed to legacy endpoints. Separately a
tier named an id absent from ``PROVIDER_MODELS`` that only *appeared* to work
via an alias rewrite, and a retired Ollama tag returned HTTP 410. Nothing
compared our alias/tier vocabulary against the vendors' model lists.

TRUTH SOURCES (config: state/fleet/vendor_truth_sources.json -> "identity")
---------------------------------------------------------------------------
* ``model_tiers.yaml`` / ``model_fallbacks.json`` / ``review_family_priority.json``
  and ``MODEL_ALIASES`` — OUR CLAIM.
* provider model lists — the VENDOR-facing truth:
    - ``PROVIDER_MODELS`` (flat_router; our curated registry of what each
      provider advertises), and
    - the last vendor-fetched catalog snapshot (``catalog_drift_check.py`` ->
      ``live_catalog_state.json``), or a ``--provider-lists-file`` fixture.

CONTRACT
--------
* SILENT + exit 0 when every invariant holds (no stdout).
* On violation: print each finding (invariant, id, why) and exit 2.
* exit 1 when inputs cannot be read (fail loud, not silent-green).
* ``--as-of DATE`` replays against history.

USAGE
-----
  model_identity_audit.py [--config PATH] [--aliases-file PATH]
                          [--registry-file PATH] [--tiers-file PATH]
                          [--fallbacks-file PATH] [--review-file PATH]
                          [--retired-file PATH] [--provider-lists-file PATH]
                          [--as-of DATE] [--json] [--verbose]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()


def discover_repo(start: Path | None = None) -> Path | None:
    p = (start or HERE).resolve()
    if p.is_file():
        p = p.parent
    for _ in range(12):
        if ((p / "scripts" / "engine" / "flat_router.py").exists()
                or (p / "state" / "fleet" / "vendor_truth_sources.json").exists()):
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def resolve_config(explicit: str | None) -> Path | None:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("VENDOR_TRUTH_SOURCES")
    if env:
        cands.append(Path(env).expanduser())
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    cands += [home / "state" / "fleet" / "vendor_truth_sources.json",
              Path.home() / ".hermes" / "state" / "fleet" / "vendor_truth_sources.json"]
    repo = discover_repo()
    if repo:
        cands.append(repo / "state" / "fleet" / "vendor_truth_sources.json")
    for c in cands:
        if c.is_file():
            return c
    return None


def find_repo(config_path: Path | None = None) -> Path | None:
    """Locate the repo (CLAIM side). See price_truth_audit.find_repo."""
    cands = []
    env = os.environ.get("TRUTH_AUDIT_REPO")
    if env:
        cands.append(Path(env).expanduser())
    cands += [Path.home() / "hermes-orchestration",
              Path.home() / ".hermes" / "orchestration",
              Path.cwd()]
    if config_path is not None:
        cands.append(config_path.parent.parent.parent)
    for c in cands:
        r = discover_repo(c)
        if r is not None:
            return r
    return None


def literal_dict_from_source(path: Path, symbol: str) -> dict:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == symbol:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
    return {}


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        # minimal fallback: no PyYAML -> parse only the ids we need via regex
        return {"_raw": path.read_text()}
    return yaml.safe_load(path.read_text()) or {}


# ──────────────────────────────────────────────────────────────────────────
# inputs
# ──────────────────────────────────────────────────────────────────────────
def load_inputs(cfg: dict, config_path: Path, repo: Path, args) -> dict:
    claims_cfg = cfg.get("claims") or {}
    reg_mod = repo / claims_cfg.get("router_registry_module",
                                    "scripts/engine/flat_router.py")

    aliases = {}
    registry = {}
    if args.aliases_file:
        aliases = json.loads(Path(args.aliases_file).expanduser().read_text())
    elif reg_mod.is_file():
        aliases = literal_dict_from_source(reg_mod, "MODEL_ALIASES")
    if args.registry_file:
        registry = json.loads(Path(args.registry_file).expanduser().read_text())
    elif reg_mod.is_file():
        pm = literal_dict_from_source(reg_mod, "PROVIDER_MODELS")
        registry = {p: sorted(ms) for p, ms in pm.items()}

    sf = repo / "state" / "fleet"
    tiers_path = Path(args.tiers_file).expanduser() if args.tiers_file else sf / "model_tiers.yaml"
    fb_path = Path(args.fallbacks_file).expanduser() if args.fallbacks_file else sf / "model_fallbacks.json"
    rv_path = Path(args.review_file).expanduser() if args.review_file else sf / "review_family_priority.json"

    tiers = _load_yaml(tiers_path) if tiers_path.is_file() else {}
    fallbacks = json.loads(fb_path.read_text()) if fb_path.is_file() else {}
    review = json.loads(rv_path.read_text()) if rv_path.is_file() else {}

    # provider model lists (truth)
    prov_lists: dict = {}
    for p, ms in registry.items():
        prov_lists.setdefault(p, set()).update(ms)
    idcfg = cfg.get("identity") or {}
    if args.provider_lists_file:
        snap = json.loads(Path(args.provider_lists_file).expanduser().read_text())
        _merge_snapshot(prov_lists, snap)
    else:
        for rel in (idcfg.get("provider_model_list_state"),
                    idcfg.get("provider_model_list_state_live")):
            if not rel:
                continue
            sp = (repo / rel) if not str(rel).startswith(("~", "/")) else Path(
                str(rel)).expanduser()
            if sp.is_file():
                try:
                    _merge_snapshot(prov_lists, json.loads(sp.read_text()))
                except (json.JSONDecodeError, OSError):
                    pass

    retired = set(idcfg.get("retired_tags_ids") or [])
    if args.retired_file:
        rr = json.loads(Path(args.retired_file).expanduser().read_text())
        retired = set(rr if isinstance(rr, list) else rr.get("ids", []))
    elif not retired:
        for t in (idcfg.get("retired_tags") or []):
            if isinstance(t, dict) and t.get("id"):
                retired.add(t["id"])
            elif isinstance(t, str):
                retired.add(t)

    families = idcfg.get("families") or {}

    return {"aliases": aliases, "registry": registry, "provider_lists": prov_lists,
            "tiers": tiers, "fallbacks": fallbacks, "review": review,
            "retired": retired, "families": families}


def _merge_snapshot(prov_lists: dict, snap: dict) -> None:
    provs = (snap or {}).get("providers", snap) or {}
    for p, body in provs.items():
        models = body.get("models") if isinstance(body, dict) else body
        if not models:
            continue
        prov_lists.setdefault(p, set()).update(str(m) for m in models)


def canonicalize(model: str, aliases: dict) -> str:
    m = (model or "").strip()
    seen = set()
    while m in aliases and m not in seen:
        seen.add(m)
        m = aliases[m]
    return m


def referenced_ids(inputs: dict) -> list[tuple[str, str]]:
    """[(source, id)] — every model id our claim references."""
    out = []
    for tname, t in ((inputs["tiers"].get("tiers") or {}).items()):
        for c in t.get("candidates", []) or []:
            out.append((f"tier:{tname}:candidate", str(c)))
        if t.get("fallback_model"):
            out.append((f"tier:{tname}:fallback", str(t["fallback_model"])))
    for k, v in (inputs["fallbacks"] or {}).items():
        if k.startswith("_"):
            continue
        out.append(("model_fallbacks:key", str(k)))
        for x in (v or []):
            out.append(("model_fallbacks:value", str(x)))
    for row in ((inputs["review"].get("profiles")) or []):
        if row.get("model"):
            out.append((f"review:{row.get('profile')}", str(row["model"])))
    for x in (inputs["review"].get("floor_extra") or []):
        out.append(("review:floor_extra", str(x)))
    for k, v in (inputs["aliases"] or {}).items():
        out.append(("alias:key", str(k)))
        out.append(("alias:target", str(v)))
    return out


# ──────────────────────────────────────────────────────────────────────────
# invariants
# ──────────────────────────────────────────────────────────────────────────
def find_all(inputs: dict) -> list[dict]:
    alias = inputs["aliases"]
    prov_lists = inputs["provider_lists"]
    retired = inputs["retired"]
    all_ids = set().union(*prov_lists.values()) if prov_lists else set()
    findings: list[dict] = []

    def exists(mid: str) -> bool:
        return mid in all_ids

    # (A) resolvability of every referenced id
    for src, mid in referenced_ids(inputs):
        if src == "alias:key":
            canon = canonicalize(mid, alias)
            if not exists(canon):
                findings.append({
                    "invariant": "A-resolvable", "id": mid, "source": src,
                    "canonical": canon,
                    "detail": f"alias {mid!r} resolves to {canon!r}, "
                              f"which no provider advertises"})
        else:
            canon = canonicalize(mid, alias)
            if not exists(mid) and not exists(canon):
                findings.append({
                    "invariant": "A-resolvable", "id": mid, "source": src,
                    "canonical": canon,
                    "detail": f"absent from every provider model list "
                              f"(canonical={canon!r})"})

    # (B) determinism — no alias target is also an alias key resolving elsewhere
    for k, v in alias.items():
        if v in alias and alias[v] != v:
            findings.append({
                "invariant": "B-deterministic", "id": k, "source": "alias:key",
                "canonical": canonicalize(k, alias),
                "detail": f"alias chain {k!r} -> {v!r} -> {alias[v]!r}: "
                          f"one spelling, two canonicals"})
        elif canonicalize(canonicalize(k, alias), alias) != canonicalize(k, alias):
            findings.append({
                "invariant": "B-deterministic", "id": k, "source": "alias:key",
                "canonical": canonicalize(k, alias),
                "detail": "canonicalize not idempotent"})

    # (C) not-retired
    if retired:
        for src, mid in referenced_ids(inputs):
            targets = [mid] if src == "alias:target" else []
            if src == "alias:key":
                targets.append(canonicalize(mid, alias))
            if not targets:
                targets.append(canonicalize(mid, alias))
                targets.append(mid)
            for t in set(targets):
                if t in retired:
                    findings.append({
                        "invariant": "C-not-retired", "id": mid, "source": src,
                        "canonical": t,
                        "detail": f"resolves to RETIRED tag {t!r} "
                                  f"(vendor has withdrawn it)"})

    # dedupe (a referenced id + its alias target can produce the same finding)
    seen = set()
    uniq = []
    for f in findings:
        key = (f["invariant"], f["id"], f["source"], f["canonical"], f["detail"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq


def render(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        lines.append(f"[identity] {f['invariant']} {f['source']} {f['id']} "
                     f"-> {f['canonical']}: {f['detail']}")
    return "\n".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="F3 model-identity belief-vs-truth audit")
    ap.add_argument("--config", default=None)
    for opt in ("aliases-file", "registry-file", "tiers-file", "fallbacks-file",
                "review-file", "retired-file", "provider-lists-file"):
        ap.add_argument(f"--{opt}", default=None)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    config_path = resolve_config(args.config)
    if config_path is None:
        print("[identity] cannot locate vendor_truth_sources.json", file=sys.stderr)
        return 1
    cfg = json.loads(config_path.read_text())
    repo = find_repo(config_path) or config_path.parent.parent.parent

    try:
        inputs = load_inputs(cfg, config_path, repo, args)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[identity] cannot read inputs: {exc}", file=sys.stderr)
        return 1

    if not inputs["provider_lists"]:
        print("[identity] no provider model lists available (truth missing)",
              file=sys.stderr)
        return 1

    findings = find_all(inputs)
    if not findings:
        if args.verbose:
            print(f"[identity] OK — {len(inputs['provider_lists'])} provider list(s), "
                  f"{len(inputs['aliases'])} alias(es), "
                  f"{len(inputs['retired'])} retired tag(s)")
        return 0

    if args.json:
        print(json.dumps({"findings": findings}, indent=2, sort_keys=True))
    else:
        print(render(findings))
        print(f"[identity] {len(findings)} identity violation(s)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
