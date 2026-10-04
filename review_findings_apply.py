#!/usr/bin/env python3
"""review_findings_apply.py — the missing caller for review_fix_emit.py.

Reviewers drop a machine-readable findings manifest per reviewed PR into
`~/.hermes/state/review_findings/`. This no-agent cron drains that directory:
for each manifest it runs `review_fix_emit.py --findings <f> --apply` (which
creates one fix card per deterministic track and appends decision findings to
the operator decisions queue), then moves the manifest to `processed/`.

Manifest schema (see review_fix_emit.py docstring):
  {repo, board, pr, head, review_url,
   tracks:[{id,title,branch,assignee,urgency,
            findings:[{class:"deterministic"|"decision",severity,file,test,
                       summary,options}]}]}

Usage:
  review_findings_apply.py [--dir DIR] [--dry-run] [--limit N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
HERMES = HOME / ".hermes"
DEFAULT_DIR = HERMES / "state" / "review_findings"
DEFAULT_EMITTER = HERMES / "scripts" / "review_fix_emit.py"


def find_manifests(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.json") if p.is_file()),
                  key=lambda p: p.stat().st_mtime)


def _emitter(path: Path) -> Path | None:
    for base in (path, Path(__file__).resolve().parent,
                 Path(__file__).resolve().parent.parent / "manager" / "scripts"):
        if (base / "review_fix_emit.py").exists():
            return base / "review_fix_emit.py"
    return None


def apply_one(manifest: Path, emitter: Path, dry_run: bool) -> dict:
    cmd = [sys.executable, str(emitter), "--findings", str(manifest)]
    if not dry_run:
        cmd.append("--apply")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    ok = p.returncode == 0
    if ok and not dry_run:
        dest = manifest.parent / "processed"
        dest.mkdir(exist_ok=True)
        try:
            shutil.move(str(manifest), str(dest / manifest.name))
        except OSError:
            pass
    return {"manifest": manifest.name, "ok": ok, "rc": p.returncode,
            "out": ((p.stdout or "") + (p.stderr or "")).strip()}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Drain review findings manifests")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    d = Path(args.dir)
    emitter = _emitter(d)
    if emitter is None:
        print("review_fix_emit.py not found", file=sys.stderr)
        return 2
    results = [apply_one(m, emitter, args.dry_run)
               for m in find_manifests(d)[:args.limit]]
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"[findings] {r['manifest']}: {'applied' if r['ok'] else 'FAIL'}"
                  f"{' (dry-run)' if args.dry_run else ''}")
            if r["out"]:
                print("  " + r["out"].replace("\n", "\n  "))
    return 1 if any(not r["ok"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
