#!/usr/bin/env python3
"""compaction_config_apply.py — context-aware gateway hygiene valve (D-136).

The gateway "hygiene" compactor fires on EITHER `0.85 * context_length` tokens
OR a fixed `compression.hygiene_hard_message_limit` message count. The fixed
400 was doing almost all compaction (138/151 events) because long autonomous
tool-heavy turns produce hundreds of tiny messages long before the 1M-token
window fills. Scale the message valve with the model's real context window,
keeping a high emergency cap so a genuine runaway transcript is still caught.

  valve = clamp(context_length // 128, 2000, 8192)

2026-09-17: DIVISOR 512→128 (cap 8000→8192). Operator DM sessions legitimately
reach ~3000 messages/day of tiny autonomous turns, well over the old 2048 valve,
so the hygiene compactor fired constantly — and each run was a NO-OP ("did not
rotate or compact in place (no session_db on the hygiene agent)", #21301),
re-firing every pass. A higher valve stops the broken path from firing while the
agent's own in-turn compressor still bounds context. Fix the hygiene persistence
path separately (Phase N2) before lowering this again.

Text-level, key-scoped edit so a secret-bearing config.yaml is never rewritten
wholesale. Also (optionally) normalise `model.context_length` via --context.

Usage:
  compaction_config_apply.py --config PATH [--config PATH ...]
                             [--context 1048576] [--json]

Registry helper:
  compaction_config_apply.py --registry model_context_registry.json \
                             --alias deepseek/deepseek-flash:1048576 [...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MIN_VALVE = 2000
MAX_VALVE = 8192
DIVISOR = 128
DEFAULT_CONTEXT = 1_048_576


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _read_context(lines: list[str]) -> int | None:
    section = None
    for ln in lines:
        if ln.strip() and _indent(ln) == 0 and ":" in ln:
            section = ln.split(":", 1)[0]
        if (section == "model" and _indent(ln) == 2
                and re.match(r"^\s*context_length:", ln)):
            try:
                return int(ln.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def valve_for(context_length: int) -> int:
    return max(MIN_VALVE, min(MAX_VALVE, context_length // DIVISOR))


def apply(path: Path, context: int | None) -> int:
    if not path.exists():
        return -1
    lines = path.read_text().splitlines()
    ctx = context or _read_context(lines) or DEFAULT_CONTEXT
    valve = valve_for(ctx)
    out, changes, section = [], 0, None
    seen = False
    for ln in lines:
        if ln.strip() and _indent(ln) == 0 and ":" in ln:
            section = ln.split(":", 1)[0]
        if section == "compression" and _indent(ln) == 2 and \
                re.match(r"^\s*hygiene_hard_message_limit:", ln):
            seen = True
            new = re.sub(r"^(\s*hygiene_hard_message_limit:)\s*.*",
                         rf"\g<1> {valve}", ln)
            if new != ln:
                changes += 1
            ln = new
        out.append(ln)
    if not seen:
        # insert under compression: (create the section if absent)
        block = [f"  hygiene_hard_message_limit: {valve}"]
        idx = None
        for i, ln in enumerate(out):
            if re.match(r"^compression:\s*$", ln):
                idx = i
                break
        if idx is None:
            out += ["compression:", *block]
        else:
            j = idx + 1
            while j < len(out) and (not out[j].strip()
                                    or _indent(out[j]) >= 2):
                j += 1
            out = out[:j] + block + out[j:]
        changes += 1
    if changes:
        Path(path).write_text("\n".join(out) + "\n")
    return changes


def apply_registry(path: Path, aliases: dict[str, int]) -> int:
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    changes = 0
    for k, v in aliases.items():
        if data.get(k) != v:
            data[k] = v
            changes += 1
    if changes:
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return changes


def _parse_aliases(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items or []:
        if ":" not in it:
            continue
        k, v = it.rsplit(":", 1)
        try:
            out[k] = int(v)
        except ValueError:
            continue
    return out


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", default=[])
    ap.add_argument("--registry")
    ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--context", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res: dict[str, int] = {}
    for c in args.config:
        res[c] = apply(Path(c).expanduser(), args.context)
    reg = None
    if args.registry:
        aliases = _parse_aliases(args.alias)
        reg = apply_registry(Path(args.registry).expanduser(), aliases)
    total = sum(v for v in res.values() if v > 0) + (reg or 0)
    if args.json:
        print(json.dumps({"configs": res, "registry": reg, "total": total},
                         indent=1))
    else:
        for c, n in res.items():
            print(f"compaction_config_apply: {c}: "
                  f"{'missing' if n < 0 else str(n) + ' change(s)'}")
        if reg is not None:
            print(f"compaction_config_apply: registry: {reg} change(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
