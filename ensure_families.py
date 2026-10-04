#!/usr/bin/env python3
"""ensure_families.py — idempotently merge MISSING family keys into a live gates.json.

WHY THIS EXISTS (2026-09-27, pr/hy4-reviewer)
Role ``15-quality-gates`` SEEDS ``{{ bot_dir }}/gates.json`` from
``scripts/governance/gates.default.json`` with ``force: no`` ("never overwrite
operator edits"). That is the right default — but it means a family-map change
landed in the repo never reaches the LIVE file once it exists. The practical
effect measured on 2026-09-27: the repo's ``families`` block gained
``tencent``/``hy4``/``hunyuan`` and ``~/.hermes/bot/gates.json`` kept the old
12-key map, so ``gate_engine.family("tencent/hy4-preview")`` kept returning the
derived string ``"tencent/hy4"`` instead of ``"tencent"`` — cosmetic for Hy4,
but a real cross-family bug for any sibling Tencent model (``tencent/hy3`` would
map to a DIFFERENT family and defeat the D-115 cross-family exclusion).

Design rules (do NOT hand-edit the live file, do NOT set ``force: yes``):
  * ADDITIVE — only keys present in the defaults and MISSING in the live file
    are added.
  * OPERATOR EDITS WIN — an existing key's value is never overwritten, even if
    the repo disagrees. Drift there is a human decision.
  * NEVER REMOVES a key.
  * IDEMPOTENT — a second run writes nothing (``changed: false``).
  * FAIL-CLOSED — a live file that exists but cannot be parsed/validated is
    reported and left UNTOUCHED (exit 2); a corrupt gate spec must not be
    silently "repaired" into something nobody reviewed.

Usage:
  ensure_families.py --gates ~/.hermes/bot/gates.json \
      --families scripts/governance/gates.default.json [--json]

Output: one JSON object on stdout
  {"changed": bool, "added": {k: v, ...}, "path": "...", "error": null|"..."}
Exit: 0 = ok (changed or not); 2 = live gates.json unreadable/corrupt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def _load_families(path: Path) -> dict:
    """The defaults' ``families`` map ({} when absent/unreadable)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    fam = data.get("families")
    return {str(k): str(v) for k, v in fam.items()} if isinstance(fam, dict) else {}


def ensure(gates_path: Path, defaults_path: Path) -> dict:
    """Merge missing family keys into ``gates_path``. Returns the report dict."""
    defaults = _load_families(defaults_path)
    report = {"changed": False, "added": {}, "path": str(gates_path),
              "error": None, "defaults_keys": len(defaults)}
    if not gates_path.exists():
        # Nothing to merge — the copy task seeds it on first run.
        report["error"] = None
        report["note"] = "gates.json absent (seed task creates it)"
        return report
    try:
        live = json.loads(gates_path.read_text())
    except (OSError, ValueError) as exc:
        report["error"] = f"unparseable gates.json: {exc}"
        return report
    if not isinstance(live, dict):
        report["error"] = "gates.json is not a JSON object"
        return report

    fam = live.get("families")
    if fam is None:
        fam = {}
        live["families"] = fam
    if not isinstance(fam, dict):
        report["error"] = "gates.json 'families' is not an object"
        return report

    added = {k: v for k, v in defaults.items() if k not in fam}
    if not added:
        return report
    fam.update(added)
    # Atomic write: a partially written gates.json would fail the next
    # load_gates() and fail-close every gate.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(gates_path.parent), prefix=".gates.json.")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(live, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, gates_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    report["changed"] = True
    report["added"] = added
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gates", default=str(Path.home() / ".hermes" / "bot" / "gates.json"))
    ap.add_argument("--families", default=str(Path(__file__).resolve().parent / "gates.default.json"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = ensure(Path(args.gates), Path(args.families))
    if args.json:
        print(json.dumps(report))
    elif report["error"]:
        print(f"ensure_families: ERROR: {report['error']}", file=sys.stderr)
    else:
        print(f"ensure_families: changed={report['changed']} added={report['added']}")
    return 2 if report["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
