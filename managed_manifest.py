#!/usr/bin/env python3
"""managed_manifest.py — load/expand the Phase L managed-file manifest (L1).

`state/fleet/managed_files.json` maps repo paths to live paths and assigns each
an authority:

  repo   — role-deployed runtime; live MUST equal origin/master (drift enforced).
  live   — authored on the node and synced into the repo; live is NEVER reverted.
  exempt — secrets/runtime artifacts; ignored.

Plus an ``out_of_scope`` list: live paths that exist on a node but map to no repo
twin in THIS repo (e.g. ``~/.hermes/bot/scripts``, tracked by the hermes-bot
repo). Declaring them — path, reason, owner — keeps the drift auditor's silence
about them honest rather than accidental (t_a15f452f).

This module turns the rule set into a flat list of
``ManagedFile(repo_path, live_path, authority, rule_id)`` for the drift auditor
(L2) and validates the manifest. Stdlib only.

CLI:
  managed_manifest.py --list [--json]   # expand and print pairs
  managed_manifest.py --validate        # schema + overlap checks
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

VALID_AUTHORITIES = ("repo", "live", "exempt")
DEFAULT_MANIFEST = "state/fleet/managed_files.json"


@dataclass(frozen=True)
class ManagedFile:
    repo_path: str
    live_path: str
    authority: str
    rule_id: str


def repo_root(start: Path | None = None) -> Path:
    """Locate the orchestration repo root (dir containing state/fleet/)."""
    if start is None:
        start = Path(__file__).resolve()
    for p in [start, *start.parents]:
        if (p / DEFAULT_MANIFEST).is_file():
            return p
    return Path(__file__).resolve().parents[2]


def load(manifest: Path | str | None = None) -> dict:
    if manifest is None:
        manifest = repo_root() / DEFAULT_MANIFEST
    with open(manifest) as fh:
        return json.load(fh)


def _match(rel: str, patterns: list[str]) -> bool:
    base = os.path.basename(rel)
    for pat in patterns:
        if pat in ("*", "**", "**/*"):
            return True
        if pat.startswith("**/"):
            sub = pat[3:]
            if fnmatch.fnmatch(base, sub) or fnmatch.fnmatch(rel, pat) \
                    or fnmatch.fnmatch(rel, sub):
                return True
        elif fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False


def is_exempt(rel: str, manifest: dict) -> bool:
    pats = (manifest.get("exempt") or {}).get("patterns") or []
    return _match(rel, pats)


def _authority_overrides(manifest: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for ov in manifest.get("overrides") or []:
        for rf in ov.get("repo_files") or []:
            out[rf] = ov.get("authority", "repo")
    return out


def _expand_rule(rule: dict, root: Path) -> list[ManagedFile]:
    rd = root / rule["repo_dir"]
    live_dir = Path(os.path.expanduser(rule["live_dir"]))
    include = rule.get("include") or ["*"]
    recurse = bool(rule.get("recurse", False))
    authority = rule.get("authority", "repo")
    rid = rule.get("id", "?")
    out: list[ManagedFile] = []
    if not rd.is_dir():
        return out
    if recurse:
        for p in sorted(rd.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            rel_in_dir = str(p.relative_to(rd))
            if not _match(rel_in_dir, include):
                continue
            out.append(ManagedFile(rel, str(live_dir / rel_in_dir), authority, rid))
    else:
        for p in sorted(rd.iterdir()):
            if not p.is_file():
                continue
            if not _match(p.name, include):
                continue
            rel = str(p.relative_to(root))
            out.append(ManagedFile(rel, str(live_dir / p.name), authority, rid))
    return out


def expand(manifest: dict | None = None, root: Path | None = None) -> list[ManagedFile]:
    manifest = manifest or load()
    root = root or repo_root()
    ov = _authority_overrides(manifest)
    entries: dict[str, ManagedFile] = {}
    for rule in manifest.get("rules") or []:
        for mf in _expand_rule(rule, root):
            if is_exempt(mf.repo_path, manifest):
                continue
            auth = ov.get(mf.repo_path, mf.authority)
            entries[mf.repo_path] = ManagedFile(
                mf.repo_path, mf.live_path, auth, mf.rule_id)
    return [entries[k] for k in sorted(entries)]


def load_out_of_scope(manifest: dict | None = None) -> list[dict]:
    """Return the ``out_of_scope`` declarations with ``~`` expanded.

    t_a15f452f: some live paths a fleet node really has (``~/.hermes/bot/scripts``,
    ``~/.hermes/bot/config``) are tracked by a DIFFERENT repo, so this manifest
    maps them to no repo twin and repo-first drift checking structurally cannot
    cover them. Declaring that explicitly — path + reason + owner — keeps the
    auditor's silence honest instead of accidental.
    """
    manifest = manifest or load()
    out: list[dict] = []
    for entry in manifest.get("out_of_scope") or []:
        row = dict(entry)
        row["path"] = str(Path(os.path.expanduser(entry.get("path", ""))))
        out.append(row)
    return out


def load_runtime_checkouts(manifest: dict | None = None) -> list[dict]:
    """Return the ``runtime_checkouts`` entries with ``~`` expanded.

    Runtime checkouts are git working trees that must be clean and on their
    authoritative ref (e.g. the gateway's editable ``~/.hermes/hermes-agent``
    checkout tracking ``fork/main``). They are not repo->live file mappings, so
    they are handled by ``repo_drift_check.py``'s runtime gate rather than
    ``expand()``.
    """
    manifest = manifest or load()
    out: list[dict] = []
    for rc in manifest.get("runtime_checkouts") or []:
        row = dict(rc)
        row["path"] = str(Path(os.path.expanduser(rc.get("path", ""))))
        out.append(row)
    return out


def validate(manifest: dict | None = None) -> list[str]:
    """Return a list of problems (empty == valid)."""
    manifest = manifest or load()
    problems: list[str] = []
    if manifest.get("version") != 1:
        problems.append("version must be 1")
    if manifest.get("default_authority") not in VALID_AUTHORITIES:
        problems.append("default_authority invalid")
    seen_rc: set[str] = set()
    for rc in manifest.get("runtime_checkouts") or []:
        rid = rc.get("id")
        if not rid:
            problems.append("runtime_checkout missing id")
        elif rid in seen_rc:
            problems.append(f"duplicate runtime_checkout id: {rid}")
        else:
            seen_rc.add(rid)
        if not rc.get("path"):
            problems.append(f"{rid}: runtime_checkout missing path")
        if rc.get("authority_ref") is not None and not isinstance(rc.get("authority_ref"), str):
            problems.append(f"{rid}: authority_ref must be a string or null")
        if not isinstance(rc.get("blocking", True), bool):
            problems.append(f"{rid}: blocking must be boolean")
    seen: set[str] = set()
    for rule in manifest.get("rules") or []:
        rid = rule.get("id")
        if not rid:
            problems.append("rule missing id")
        elif rid in seen:
            problems.append(f"duplicate rule id: {rid}")
        else:
            seen.add(rid)
        if rule.get("kind") != "dir":
            problems.append(f"{rid}: only kind=dir is supported")
        if rule.get("authority") not in VALID_AUTHORITIES:
            problems.append(f"{rid}: invalid authority {rule.get('authority')!r}")
        for key in ("repo_dir", "live_dir"):
            if not rule.get(key):
                problems.append(f"{rid}: missing {key}")
        rd = rule.get("repo_dir", "")
        if rd.startswith("/") or ".." in Path(rd).parts:
            problems.append(f"{rid}: repo_dir must be a repo-relative path")
    for ov in manifest.get("overrides") or []:
        if ov.get("authority") not in VALID_AUTHORITIES:
            problems.append(f"override {ov.get('id')}: invalid authority")
    seen_oos: set[str] = set()
    for entry in manifest.get("out_of_scope") or []:
        oid = entry.get("id")
        if not oid:
            problems.append("out_of_scope entry missing id")
        elif oid in seen_oos:
            problems.append(f"duplicate out-of-scope id: {oid}")
        else:
            seen_oos.add(oid)
        if not entry.get("path"):
            problems.append(f"out_of_scope {oid}: missing path")
        if not str(entry.get("reason") or "").strip():
            problems.append(f"out_of_scope {oid}: empty reason")
        if not str(entry.get("owner") or "").strip():
            problems.append(f"out_of_scope {oid}: empty owner")
    return problems


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--manifest")
    args = ap.parse_args(argv)

    man = load(args.manifest) if args.manifest else load()
    if args.validate:
        problems = validate(man)
        if problems:
            for p in problems:
                print(f"INVALID: {p}", file=sys.stderr)
            return 1
        print("manifest valid")
        return 0
    if args.list:
        entries = expand(man)
        if args.json:
            json.dump([asdict(m) for m in entries], sys.stdout, indent=2)
            print()
        else:
            for m in entries:
                print(f"{m.authority:6} {m.repo_path} -> {m.live_path} [{m.rule_id}]")
        oos = load_out_of_scope(man)
        if oos:
            if args.json:
                pass
            else:
                print(f"\n# out of scope ({len(oos)}) — live paths with no repo twin "
                      "in this repo; drift checking cannot cover them:")
                for e in oos:
                    print(f"  {e['path']}  [{e['id']}] owner={e['owner']}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
