#!/usr/bin/env python3
"""cron_script_guard.py — no_agent watchdog: an ENABLED cron job that names a
script the scheduler can never resolve.

Why this exists (card t_a8401ded, 2026-09-23)
---------------------------------------------
`cron/scheduler.py` resolves a relative ``script:`` under
``_get_hermes_home()/scripts`` (the PROFILE scripts dir, e.g.
``~/.hermes/profiles/manager/scripts``) and REFUSES any absolute path that
escapes that directory ("path traversal guard"). Two ansible roles installed
their scripts into ``~/.hermes/scripts`` instead, so six ENABLED jobs failed on
every tick with ``Script not found: ...``. Because those jobs are
``deliver=local``, the scheduler wrote the failure into
``cron/output/<id>/<ts>.md`` and pushed it nowhere; the only alert cron read a
different job's output, so the defect hid for days at a 15-minute cadence.

Contract
--------
* Empty stdout == healthy (the no_agent silent contract). Nothing is printed
  when every enabled job's script resolves.
* Every finding is human-readable, multi-line, and printed for ``deliver=origin``
  cron delivery. Static resolution is the primary signal (deterministic, no
  output-file heuristics); the newest cron output is read only to add the
  captured failure line as evidence.
* Always exit 0: a non-zero exit is itself delivered verbatim as a cron failure.

Env overrides (tests): CRON_GUARD_JOBS, CRON_GUARD_SCRIPTS_DIR,
CRON_GUARD_OUTPUT_DIR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
MANAGER = HOME / ".hermes" / "profiles" / "manager"

JOBS = Path(os.environ.get("CRON_GUARD_JOBS") or MANAGER / "cron" / "jobs.json")
SCRIPTS_DIR = Path(os.environ.get("CRON_GUARD_SCRIPTS_DIR") or MANAGER / "scripts")
OUTPUT_DIR = Path(os.environ.get("CRON_GUARD_OUTPUT_DIR") or MANAGER / "cron" / "output")


def load_jobs(path) -> list:
    """jobs.json as a list, tolerating dict-wrapped or dict-keyed shapes."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return []
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return jobs or []


def resolve(script: str, scripts_dir) -> Path:
    """Mirror scheduler resolution: expanduser, relative => <scripts_dir>/name."""
    p = Path(script).expanduser()
    return p if p.is_absolute() else Path(scripts_dir) / p


def escape_report(script: str, scripts_dir) -> str | None:
    """The scheduler's traversal guard, restated: an absolute script that is not
    inside scripts_dir is blocked before the existence check."""
    p = Path(script).expanduser()
    if not p.is_absolute():
        return None
    base = Path(scripts_dir).resolve()
    try:
        p.resolve().relative_to(base)
    except ValueError:
        return f"outside the scheduler's scripts dir ({base})"
    return None


def newest_output(output_dir, job_id: str):
    d = Path(output_dir) / str(job_id)
    if not d.is_dir():
        return None, ""
    files = sorted((f for f in d.glob("*.md") if f.is_file()),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None, ""
    try:
        return files[0], files[0].read_text(errors="replace")
    except OSError:
        return files[0], ""


def failure_evidence(text: str) -> str:
    """The captured scheduler failure line, if the newest output carries one."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("Script not found:", "Blocked:")):
            return s
    if "**Status:** script failed" in text:
        return "**Status:** script failed"
    return ""


def findings(jobs, scripts_dir=SCRIPTS_DIR, output_dir=OUTPUT_DIR) -> list:
    out = []
    for j in jobs:
        if not j.get("enabled"):
            continue
        script = j.get("script")
        if not script:
            continue  # LLM/prompt jobs have no script to resolve
        path = resolve(script, scripts_dir)
        escaped = escape_report(script, scripts_dir)
        problem = None
        if escaped:
            problem = f"script {script!r} resolves {escaped}"
        elif not path.exists():
            problem = f"script missing: {path}"
        elif not path.is_file():
            problem = f"script path is not a file: {path}"
        if not problem:
            continue
        job_id = j.get("id", "?")
        name = j.get("name") or job_id
        ofile, otext = newest_output(output_dir, job_id)
        ev = failure_evidence(otext)
        out.append({
            "job": name, "id": job_id, "script": script, "problem": problem,
            "deliver": j.get("deliver"), "schedule": (j.get("schedule") or {}).get("display"),
            "evidence_file": None if ofile is None else str(ofile),
            "evidence": ev,
        })
    return out


def render(items: list) -> str:
    if not items:
        return ""
    lines = [f"⚠️ CRON-SCRIPT: {len(items)} enabled cron job(s) cannot run their script"
             " — every tick fails silently when deliver=local"]
    for it in items:
        lines.append(f"  • {it['job']} ({it['id']}, {it['schedule']}, deliver={it['deliver']})")
        lines.append(f"    {it['problem']}")
        if it["evidence"]:
            lines.append(f"    last run captured: {it['evidence']}")
            lines.append(f"    ({it['evidence_file']})")
    lines.append("  fix: install the script into the profile scripts dir the scheduler uses"
                 " (relative `script:`), or pause the job.")
    return "\n".join(lines)


def main() -> int:
    print(render(findings(load_jobs(JOBS))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
