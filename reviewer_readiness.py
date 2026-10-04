#!/usr/bin/env python3
"""reviewer_readiness.py — is at least one cross-family reviewer lane live?

Writes ~/.hermes/bot/reviewer_readiness.json:

    {"ready": true|false|null, "matched": [...], "expected": N,
     "served": M, "ts": <epoch>, "source": "..."}

`ready` is:
  * true  — >=1 reviewer-tier candidate is served by the live router;
  * false — the router answered and NO reviewer candidate is served (the
            "qwen3.5:397b has no lane" failure class);
  * null  — the router was unreachable (unknown; callers fail-open).

Read by gateway.dispatch_headroom.reviewer_headroom() so the dispatcher holds
workers when the fleet has no way to review their output (2026-09-30).

Usage: reviewer_readiness.py [--json]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
OUT = BOT / "reviewer_readiness.json"
ROUTER = os.environ.get("HERMES_ROUTER", "http://127.0.0.1:9099")


def _expected_reviewer_models() -> set[str]:
    ids: set[str] = set()
    try:
        import yaml
        tiers = (yaml.safe_load((BOT / "model_tiers.yaml").read_text()) or {}).get("tiers") or {}
        for name, spec in tiers.items():
            if not str(name).startswith("tier/review-"):
                continue
            if not isinstance(spec, dict):
                continue
            for c in (spec.get("candidates") or []) + [spec.get("fallback_model")]:
                if c:
                    ids.add(str(c))
    except Exception:
        pass
    try:
        prio = json.loads((BOT / "review_family_priority.json").read_text())
        for row in prio.get("profiles", []):
            m = row.get("model")
            if m:
                ids.add(str(m))
    except Exception:
        pass
    return ids


def _norm(m: str) -> str:
    m = str(m).strip().lower()
    return m.split("/")[-1]


def _served_models() -> set[str] | None:
    try:
        with urllib.request.urlopen(f"{ROUTER}/v1/models", timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {_norm(x.get("id")) for x in data.get("data", []) if x.get("id")}
    except Exception:
        return None


def main(argv: list[str]) -> int:
    expected = _expected_reviewer_models()
    served = _served_models()
    if served is None:
        ready = None
        matched: list[str] = []
    else:
        matched = sorted({e for e in expected if _norm(e) in served})
        ready = bool(matched)
    out = {
        "ready": ready,
        "matched": matched,
        "expected": len(expected),
        "served": 0 if served is None else len(served),
        "ts": time.time(),
        "source": "router /v1/models" if served is not None else "router unreachable",
    }
    try:
        BOT.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(OUT)
    except OSError:
        pass
    if "--json" in argv:
        print(json.dumps(out, indent=1))
    else:
        print(f"reviewer_readiness: ready={ready} matched={matched[:6]} "
              f"expected={len(expected)} served={out['served']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
