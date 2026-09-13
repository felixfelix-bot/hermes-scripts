#!/usr/bin/env python3
"""verify_skill_loads.py — T3.3 gate: the patched skill must still LOAD.

Applies patches/kanban-worker_quota-pause-taxonomy.patch to a throwaway copy of
a real skills tree and asserts, through Hermes' runtime manifest scanner (the
only ground truth for skill discovery — `hermes skills list` lies for symlinks),
that the kanban-worker skill is still discoverable with the exact same key set
as the unpatched tree, and that the taxonomy text is present in the loaded file.

Exit codes: 0 = ok, 1 = FAIL, 3 = SKIP (no local skills tree / scanner).

Usage: python3 tests/verify_skill_loads.py [--skill-src <SKILL.md>]
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches/kanban-worker_quota-pause-taxonomy.patch"
REL = "devops/kanban-worker/SKILL.md"
MARKER = "### Special prefix: `quota-paused:`"
HERMES_AGENT = pathlib.Path(os.environ.get("HERMES_AGENT_DIR", str(pathlib.Path.home() / ".hermes/hermes-agent")))


def fail(msg: str) -> None:
    print(f"FAIL- {msg}")
    sys.exit(1)


def manifest(skills_root: pathlib.Path):
    sys.path.insert(0, str(HERMES_AGENT))
    try:
        from agent.prompt_builder import _build_skills_manifest  # type: ignore
    except Exception as exc:  # scanner not available -> caller reports SKIP
        print(f"SKIP- runtime manifest scanner unavailable: {exc}")
        sys.exit(3)
    return _build_skills_manifest(skills_root)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill-src", default=str(pathlib.Path.home() / ".hermes/skills" / REL))
    args = ap.parse_args()

    src = pathlib.Path(args.skill_src)
    if not src.is_file():
        print(f"SKIP- base skill not present on this host: {src}")
        return 3
    if not PATCH.is_file():
        fail(f"patch file missing: {PATCH}")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="t33-load-"))
    try:
        # bare tree (unpatched) + patched tree, both under a skills root
        bare = tmp / "bare" / "skills"
        patched = tmp / "patched" / "skills"
        for root in (bare, patched):
            (root / REL).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / REL)

        r = subprocess.run(
            ["patch", "-p1", "--batch", "--fuzz=0", "-i", str(PATCH)],
            cwd=patched, capture_output=True, text=True,
        )
        if r.returncode != 0:
            fail(f"patch did not apply to the copied tree (rc={r.returncode}): {r.stdout.strip()} {r.stderr.strip()}")

        text = (patched / REL).read_text(encoding="utf-8")
        if MARKER not in text:
            fail("taxonomy marker absent from the patched skill")
        if not text.startswith("---\n") or text.count("\n---\n", 0, 4000) < 1:
            fail("frontmatter block broken by the patch")

        m_bare = manifest(bare)
        m_patched = manifest(patched)
        bare_keys = {k for k in m_bare if "kanban-worker" in k}
        patched_keys = {k for k in m_patched if "kanban-worker" in k}
        if not bare_keys:
            fail("unpatched control tree did not expose the skill (test setup problem)")
        if patched_keys != bare_keys:
            fail(f"skill discovery changed: {bare_keys} -> {patched_keys}")
        if set(m_bare) != set(m_patched):
            fail("patched tree exposes a different skill set than the control tree")

        print(
            "ok  - skill still loads: found %s in the runtime manifest (size %s -> %s bytes)"
            % (sorted(patched_keys), sorted(m_bare.values())[0][1] if m_bare else "?",
               sorted(m_patched.values())[0][1] if m_patched else "?")
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
