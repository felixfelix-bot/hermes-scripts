#!/usr/bin/env python3
"""reviewer_coverage_check.py — every review family must have >=2 lanes (G6).

The "Qwen+GLM never ran" incident happened because a family had a single lane
(ollama) and it went dry. This check asserts, for each `tier/review-<family>`:
  * >= MIN_CANDIDATES candidate models meet the tier's benchmark floor;
  * those candidates span >= MIN_PROVIDERS distinct providers (best-effort, via
    flat_router.PROVIDER_MODELS when importable).

Report-only + operator alert. Exit 0 always.
Usage: reviewer_coverage_check.py [--json] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
MIN_CANDIDATES = int(os.environ.get("REVIEWER_MIN_CANDIDATES", "2"))
MIN_PROVIDERS = int(os.environ.get("REVIEWER_MIN_PROVIDERS", "2"))

_CODING = {"economy": 0, "solid": 1, "pro": 2}
_TOOL = {"ok": 0, "good": 1, "strong": 2}


def _yaml(name: str) -> dict:
    try:
        import yaml
        return yaml.safe_load((BOT / name).read_text()) or {}
    except Exception:
        return {}


def _meets_floor(rec: dict, tier: dict) -> bool:
    cc = _CODING.get(str(rec.get("coding_class", "economy")).lower(), 0)
    tu = _TOOL.get(str(rec.get("tool_use", "ok")).lower(), 0)
    ctx = int(rec.get("context_window", 0) or 0)
    return (cc >= _CODING.get(str(tier.get("min_coding_class", "solid")).lower(), 1)
            and tu >= _TOOL.get(str(tier.get("min_tool_use", "good")).lower(), 1)
            and ctx >= int(tier.get("min_context", 0) or 0))


def _provider_family(lane: str) -> str:
    """Collapse lane suffixes so ollama_cloud_2/_3 count as ONE provider family."""
    import re
    return re.sub(r"_\d+$", "", str(lane or ""))


def _provider_map() -> dict:
    """model -> set(provider FAMILIES), from flat_router.PROVIDER_MODELS."""
    import contextlib
    import io
    try:
        sys.path.insert(0, str(BOT))
        with contextlib.redirect_stdout(io.StringIO()):
            import flat_router as fr  # type: ignore
        out: dict = {}
        for lane, models in (getattr(fr, "PROVIDER_MODELS", {}) or {}).items():
            fam = _provider_family(lane)
            for m in models:
                out.setdefault(m, set()).add(fam)
        return out
    except Exception:
        return {}


def coverage() -> list[dict]:
    bench = (_yaml("model_benchmarks.yaml").get("models") or {})
    tiers = (_yaml("model_tiers.yaml").get("tiers") or {})
    pmap = _provider_map()
    out = []
    for tname, t in tiers.items():
        if not str(tname).startswith("tier/review-"):
            continue
        quals, provs = [], set()
        for cand in (t.get("candidates") or []):
            rec = bench.get(cand)
            if rec is None:
                for _m, r in bench.items():
                    if cand in (r.get("reported_as") or []):
                        rec = r
                        break
            if rec and _meets_floor(rec, t):
                quals.append(cand)
                provs |= pmap.get(cand, set())
        ok = (len(quals) >= MIN_CANDIDATES
              and (not pmap or len(provs) >= MIN_PROVIDERS))
        out.append({"tier": tname, "qualified": quals,
                    "providers": sorted(provs), "ok": ok})
    return out


def _alert(text: str) -> str:
    try:
        sys.path.insert(0, str(HERMES / "scripts"))
        from operator_alert import post_alert  # type: ignore
        return post_alert(text, topic="reviewer-coverage", cooldown_s=86400)
    except Exception:
        return "unconfigured"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rows = coverage()
    bad = [r for r in rows if not r["ok"]]
    if args.json:
        print(json.dumps({"families": rows, "starved": bad}, indent=1))
    else:
        print(f"reviewer-coverage-check: {len(rows)} review tier(s), {len(bad)} starved")
        for r in rows:
            print(f"  {'OK ' if r['ok'] else 'BAD'} {r['tier']}: "
                  f"{len(r['qualified'])} qualified, providers={r['providers']}")
    if bad and not args.dry_run:
        _alert("⚠️ reviewer family coverage:\n" + "\n".join(
            f"- {r['tier']}: {len(r['qualified'])} floor-qualified, "
            f"providers={r['providers']}" for r in bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
