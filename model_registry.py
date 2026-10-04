#!/usr/bin/env python3
"""model_registry.py — the single source of truth for LLM *choices* (Phase M).

Model IDs age fast (glm-5.2 → glm-5.3, glm-4.5 removed, deepseek v4.1 …). Hard
coding a chosen model in code or tests guarantees drift. This module centralises
every *choice* behind the config-as-code registries:

  * ``state/fleet/model_tiers.yaml``        tier -> candidates + fallback_model
  * ``state/fleet/model_benchmarks.yaml``   per-model capability snapshot
  * ``state/fleet/model_fallbacks.json``    model -> fallback chain
  * ``state/fleet/review_family_priority.json``  reviewer profiles (cost-ordered)
  * ``scripts/governance/gates.default.json``    family prefix map

Catalogs (a provider's advertised model list, e.g. ``zai_proxy.PROVIDER_MODELS``)
are NOT choices and stay where they are. Anything that must *pick* a model goes
through here.

CLI:
  model_registry.py --tiers [--json]          # tier -> fallback_model + candidates
  model_registry.py --recommended [--json]    # flat "recommended models" list
  model_registry.py --fallback <model>        # fallback chain for a model
  model_registry.py --family <model>          # family of a model id
  model_registry.py --reviewers [--json]      # cost-ordered reviewer profiles
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is a fleet dependency
    yaml = None

DEFAULT_TIERS = "state/fleet/model_tiers.yaml"
DEFAULT_BENCH = "state/fleet/model_benchmarks.yaml"
DEFAULT_FALLBACKS = "state/fleet/model_fallbacks.json"
DEFAULT_REVIEWERS = "state/fleet/review_family_priority.json"
DEFAULT_GATES = "scripts/governance/gates.default.json"


def repo_root(start: Path | None = None) -> Path:
    if start is None:
        start = Path(__file__).resolve()
    for p in [start, *start.parents]:
        if (p / DEFAULT_TIERS).is_file():
            return p
    return Path(__file__).resolve().parents[2]


def _read_yaml(path: Path) -> dict:
    if yaml is None or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Registry:
    """Lazily-loaded view over the fleet model-choice configs."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else repo_root()

    # -- raw configs --------------------------------------------------------
    def tiers_cfg(self) -> dict:
        return _read_yaml(self.root / DEFAULT_TIERS)

    def benchmarks(self) -> dict:
        return _read_yaml(self.root / DEFAULT_BENCH)

    def fallbacks_map(self) -> dict:
        return _read_json(self.root / DEFAULT_FALLBACKS)

    def reviewers_cfg(self) -> dict:
        return _read_json(self.root / DEFAULT_REVIEWERS)

    def families_map(self) -> dict:
        gates = _read_json(self.root / DEFAULT_GATES)
        fam = gates.get("families")
        return fam if isinstance(fam, dict) else {}

    # -- tier accessors -----------------------------------------------------
    def tiers(self) -> dict:
        t = self.tiers_cfg().get("tiers")
        return t if isinstance(t, dict) else {}

    def tier_candidates(self, tier: str) -> list[str]:
        spec = self.tiers().get(tier) or {}
        c = spec.get("candidates")
        return [str(x) for x in c] if isinstance(c, list) else []

    def fallback_model(self, tier: str) -> str | None:
        spec = self.tiers().get(tier) or {}
        fb = spec.get("fallback_model")
        if fb:
            return str(fb)
        cands = self.tier_candidates(tier)
        return cands[0] if cands else None

    def tier_model(self, tier: str) -> str | None:
        """The concrete model currently recommended for a tier (its fallback)."""
        return self.fallback_model(tier)

    # -- model accessors ----------------------------------------------------
    def fallbacks(self, model: str) -> list[str]:
        fb = self.fallbacks_map().get(model)
        return [str(x) for x in fb] if isinstance(fb, list) else []

    def benchmark(self, model: str) -> dict:
        models = self.benchmarks().get("models") or {}
        rec = models.get(model)
        if isinstance(rec, dict):
            return rec
        for _mid, r in models.items():
            if isinstance(r, dict) and model in (r.get("reported_as") or []):
                return r
        return {}

    def family(self, model: str) -> str:
        """Map a model id to an LLM family (mirrors gate_engine.family)."""
        families = self.families_map()
        m = (model or "").lower()
        if not m:
            return "unknown"
        for key in sorted(families, key=len, reverse=True):
            if m.startswith(key) or f"/{key}" in m or f"-{key}" in m:
                return str(families[key])
        return m.split("-")[0].split(":")[0] or "unknown"

    # -- reviewer accessors -------------------------------------------------
    def reviewers(self) -> list[dict]:
        profs = self.reviewers_cfg().get("profiles")
        return [p for p in profs if isinstance(p, dict)] if isinstance(profs, list) else []

    def reviewer_floor_extra(self) -> list[str]:
        extra = self.reviewers_cfg().get("floor_extra")
        return [str(x) for x in extra] if isinstance(extra, list) else []

    # -- client hints (ported from model_tier_router, Phase M4) --------------
    def client_hints(self) -> dict:
        h = self.tiers_cfg().get("client_hints")
        return {str(k).lower(): str(v) for k, v in h.items()} if isinstance(h, dict) else {}

    def client_hint_tier(self, hint: str | None) -> str | None:
        """Map an ``X-Model-Tier``-style client hint to a fleet tier (or None).

        Replaces the dead ``model_tier_router.CLIENT_HINT_ALIASES`` with a
        config-driven map (``client_hints`` in ``model_tiers.yaml``), so the
        vocabulary lives with the rest of the model choices.
        """
        if not hint:
            return None
        return self.client_hints().get(str(hint).lower().strip())

    def usage_mix_target(self) -> dict:
        m = self.tiers_cfg().get("usage_mix")
        if not isinstance(m, dict):
            return {"enabled": False, "economy_pct": 10, "premium_pct": 10}
        return {
            "enabled": bool(m.get("enabled", False)),
            "economy_pct": float(m.get("economy_pct", 10) or 10),
            "premium_pct": float(m.get("premium_pct", 10) or 10),
        }

    # -- aggregate ----------------------------------------------------------
    def recommended(self) -> list[dict]:
        """Flat, deduped list of every model the fleet currently recommends.

        Each entry: {model, source} where source is ``tier/<name>`` (fallback or
        candidate) or ``reviewer/<family>``. Source order is stable; within a
        tier, fallback_model comes before its candidates.
        """
        seen: dict[str, str] = {}
        for tier in sorted(self.tiers()):
            fb = self.fallback_model(tier)
            if fb:
                seen.setdefault(fb, f"{tier} (fallback)")
            for cand in self.tier_candidates(tier):
                seen.setdefault(cand, f"{tier} (candidate)")
        for prof in self.reviewers():
            m = prof.get("model")
            if m:
                seen.setdefault(str(m), f"reviewer/{prof.get('family', '?')}")
        for m in self.reviewer_floor_extra():
            seen.setdefault(m, "reviewer floor_extra")
        return [{"model": m, "source": s} for m, s in seen.items()]


# ---------------------------------------------------------------------------
# Module-level convenience (default root)
# ---------------------------------------------------------------------------

def tier_candidates(tier: str) -> list[str]:
    return Registry().tier_candidates(tier)


def fallback_model(tier: str) -> str | None:
    return Registry().fallback_model(tier)


def tier_model(tier: str) -> str | None:
    return Registry().tier_model(tier)


def fallbacks(model: str) -> list[str]:
    return Registry().fallbacks(model)


def family(model: str) -> str:
    return Registry().family(model)


def recommended() -> list[dict]:
    return Registry().recommended()


def usage_mix_thresholds(samples, economy_pct: float = 10.0,
                         premium_pct: float = 10.0) -> dict | None:
    """Percentile thresholds steering a target usage mix (ported M4).

    Pure port of ``adaptive_model_tuner.compute_percentile_thresholds``: given
    historic headroom samples, return the low/high cut points that put
    ``economy_pct``/``premium_pct`` of the mass on the cheap/expensive tiers.
    Returns None when there is no data (caller keeps its current thresholds).
    """
    vals = sorted(float(s) for s in samples if s is not None)
    if not vals:
        return None
    n = len(vals)
    lo = max(0, min(int(n * (economy_pct / 100.0)), n - 1))
    hi = min(n - 1, max(int(n * ((100.0 - premium_pct) / 100.0)), 0))
    return {
        "economy_max_hours": round(max(vals[lo], 0.1), 1),
        "premium_min_hours": round(vals[hi], 1),
        "samples": n,
    }


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--tiers", action="store_true")
    ap.add_argument("--recommended", action="store_true")
    ap.add_argument("--reviewers", action="store_true")
    ap.add_argument("--fallback", metavar="MODEL")
    ap.add_argument("--family", metavar="MODEL")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    reg = Registry(args.root) if args.root else Registry()
    if args.fallback is not None:
        print(json.dumps(reg.fallbacks(args.fallback)))
        return 0
    if args.family is not None:
        print(reg.family(args.family))
        return 0
    if args.recommended:
        rec = reg.recommended()
        if args.json:
            print(json.dumps(rec, indent=2))
        else:
            for r in rec:
                print(f"{r['model']:40} {r['source']}")
        return 0
    if args.reviewers:
        revs = reg.reviewers()
        if args.json:
            print(json.dumps(revs, indent=2))
        else:
            for p in revs:
                print(f"{p.get('profile','?'):24} {p.get('family','?'):10} {p.get('model','?')}")
        return 0
    if args.tiers:
        out = {t: {"fallback_model": reg.fallback_model(t),
                   "candidates": reg.tier_candidates(t)}
               for t in sorted(reg.tiers())}
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for t, v in out.items():
                print(f"{t:24} {v['fallback_model']}  {v['candidates']}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
