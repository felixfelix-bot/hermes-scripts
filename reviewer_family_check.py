#!/usr/bin/env python3
"""Cron wrapper installed by role 15-quality-gates (D-132).

A *real file* inside <HERMES_HOME>/scripts/ so the Hermes cron runner's
"script path resolves outside the scripts directory" check accepts it, while
the canonical implementation stays in one place: ~/.hermes/bot/governance/.
The wrapper locates that directory by walking up from its own location (works
for both the global scripts dir and a profile's scripts dir). Do not replace
this with a symlink - that is exactly the path-check failure.
"""
import os
import runpy
from pathlib import Path

os.environ.setdefault("NGIT_CI_EVIDENCE_BIN", "/home/c03rad0r/.hermes/scripts/ngit_ci_evidence.py")
os.environ.setdefault("GATES_ENFORCE_SINCE_TS", "1789309258")
MODULE = "reviewer_family_check.py"


def _find_canon():
    roots = []
    env = os.environ.get("HERMES_HOME")
    if env:
        p = Path(env).expanduser()
        if "profiles" in p.parts:
            i = p.parts.index("profiles")
            p = Path(*p.parts[:i]) if i else Path(os.sep)
        roots.append(p)
    roots.append(Path(__file__).resolve().parent)
    for r in roots:
        cur = r
        for _ in range(6):
            cand = cur / "bot" / "governance" / MODULE
            if cand.exists():
                return cand
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


CANON = _find_canon()
if CANON is None:
    raise SystemExit("gate wrapper: canonical module not found: %s" % MODULE)
runpy.run_path(str(CANON), run_name="__main__")
