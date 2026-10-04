#!/usr/bin/env python3
"""router_config_apply.py — idempotently align model + aux routing config.

D-134. Text-level, key-scoped edits so a secret-bearing config.yaml is never
rewritten wholesale:
  * model.default            -> the V4.1 canonical (deepseek/deepseek-flash)
  * auxiliary.*.base_url     -> the local router (http://127.0.0.1:9099)
  * auxiliary.*.model        -> the cheap canonical (except vision/tts which
                                stay on the vision models)
  * compression.abort_on_summary_failure -> false

Usage:
  router_config_apply.py --config PATH [--config PATH ...]
                         [--model deepseek/deepseek-flash]
                         [--base-url http://127.0.0.1:9099]
                         [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Aux flows whose model we deliberately DO change to the cheap canonical.
CHEAP_AUX = {
    "web_extract", "compression", "skills_hub", "approval", "mcp",
    "title_generation", "triage_specifier", "kanban_decomposer",
    "profile_describer", "curator", "monitor", "tts_audio_tags",
}
# Aux flows left untouched (need a vision/audio model).
KEEP_AUX = {"vision"}


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def apply(path: Path, model: str, base_url: str) -> int:
    if not path.exists():
        return -1
    lines = path.read_text().splitlines()
    out, changes = [], 0
    section = None            # "model" | "auxiliary" | None
    aux_key = None            # current auxiliary.<key>
    for ln in lines:
        if not ln.strip():
            out.append(ln); continue
        if _indent(ln) == 0:
            section = ln.split(":", 1)[0] if ":" in ln else None
            aux_key = None
        if section == "model" and _indent(ln) == 2 and re.match(r"^\s*default:", ln):
            new = re.sub(r"^\s*default:.*", f"  default: {model}", ln)
            if new != ln: changes += 1
            ln = new
        elif section == "auxiliary" and _indent(ln) == 2 and ln.rstrip().endswith(":"):
            aux_key = ln.strip().rstrip(":")
        elif section == "auxiliary" and _indent(ln) == 4 and aux_key in CHEAP_AUX:
            if re.match(r"^\s*base_url:", ln) and base_url:
                new = re.sub(r"^\s*base_url:.*", f'    base_url: {base_url}', ln)
                if new != ln: changes += 1
                ln = new
            elif re.match(r"^\s*model:", ln):
                new = re.sub(r"^\s*model:.*", f"    model: {model}", ln)
                if new != ln: changes += 1
                ln = new
        if "abort_on_summary_failure: true" in ln:
            ln = ln.replace("abort_on_summary_failure: true",
                            "abort_on_summary_failure: false"); changes += 1
        out.append(ln)
    if changes:
        path.write_text("\n".join(out) + "\n")
    return changes


def _registry_default_model() -> str:
    """Registry-derived default (Phase M3): no hardcoded model choice here.

    Resolves the coding-worker tier's fallback_model from the version-controlled
    registry; falls back to an operator env override. Returns "" if unavailable.
    """
    try:
        import model_registry  # deployed alongside this script
        m = model_registry.fallback_model("tier/coding-worker")
        if m:
            return m
    except Exception:
        pass
    return os.environ.get("HERMES_DEFAULT_MODEL", "").strip()


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", required=True)
    ap.add_argument("--model", default=None,
                    help="canonical model default (default: registry coding-worker fallback)")
    ap.add_argument("--base-url", default="http://127.0.0.1:9099")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.model:
        args.model = _registry_default_model()
    if not args.model:
        print("router_config_apply: no --model and registry unavailable", file=sys.stderr)
        return 2
    res = {}
    for c in args.config:
        cfg_path = Path(c).expanduser()
        n = apply(cfg_path, args.model, args.base_url)
        res[c] = n
    total = sum(v for v in res.values() if v > 0)
    if args.json:
        print(json.dumps({"changes": res, "total": total}, indent=1))
    else:
        for c, n in res.items():
            print(f"router_config_apply: {c}: "
                  f"{'missing' if n < 0 else str(n) + ' change(s)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
