#!/usr/bin/env python3
"""display_config_apply.py — idempotently align operator-visibility display config.

D-135, revised D-139. Text-level, key-scoped edits so a secret-bearing config.yaml
is never rewritten wholesale (same discipline as router_config_apply.py):
  * agent.gateway_notify_interval        -> --interval (default 300 = 5 min)
  * display.platforms.signal.*           -> long_running_notifications: false
                                            interim_assistant_messages: false
                                            busy_ack_detail: false
                                            tool_progress: 'off'

Signal is a Tier-LOW platform (no message editing), so every heartbeat is a NEW
permanent bubble — the "⏳ Working — N min — iteration x/y" spam. The operator
asked for silence until a meaningful response: these explicit per-platform values
turn the heartbeat, interim commentary and busy-ack detail OFF, so a long turn
sends nothing until the final answer. `display_config_verify.py` + the role-29
assert (D-139) fail the deploy if these are not false. Other platforms untouched.

Usage:
  display_config_apply.py --config PATH [--config PATH ...]
                          [--interval 300] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SIGNAL_KEYS = {
    "long_running_notifications": "false",
    "interim_assistant_messages": "false",
    "busy_ack_detail": "false",
    "tool_progress": "'off'",
}


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _set_interval(lines: list[str], interval: int) -> tuple[list[str], int]:
    out, changes, section = [], 0, None
    for ln in lines:
        st = ln.strip()
        if st and _indent(ln) == 0 and ":" in st:
            section = st.split(":", 1)[0]
        if (section == "agent" and _indent(ln) == 2
                and re.match(r"^\s*gateway_notify_interval:", ln)):
            new = re.sub(r"^(\s*gateway_notify_interval:)\s*.*",
                         rf"\g<1> {interval}", ln)
            if new != ln:
                changes += 1
            ln = new
        out.append(ln)
    return out, changes


def _find_block(lines: list[str], start: int, want_indent: int,
                key_re: str) -> int | None:
    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.strip() and _indent(ln) < want_indent:
            return None
        if re.match(key_re, ln):
            return i
    return None


def _signal_block(indent: int) -> list[str]:
    pad = " " * indent
    child = " " * (indent + 2)
    return [f"{pad}signal:"] + [
        f"{child}{k}: {v}" for k, v in SIGNAL_KEYS.items()
    ]


def _apply_signal(lines: list[str]) -> tuple[list[str], int]:
    changes = 0
    disp = _find_block(lines, 0, 0, r"^display:\s*$")
    if disp is None:
        # No `display:` block at all — a minimal worker config.yaml (dq05) has
        # only model/kanban/compression, so there is nothing to edit and the
        # D-139 "Signal is quiet" verify would fail. Create the block so the
        # guarantee holds on every fleet host.
        block = ["display:", "  platforms:"] + _signal_block(4)
        if lines and lines[-1].strip():
            lines = lines + [""]
        return lines + block, 1
    plat = _find_block(lines, disp + 1, 2, r"^  platforms:\s*$")
    if plat is None:
        # No display.platforms yet — create it right after `display:`.
        block = ["  platforms:"] + _signal_block(4)
        return lines[:disp + 1] + block + lines[disp + 1:], 1

    # End of the platforms section = first later non-empty line at indent <= 2.
    end = len(lines)
    for i in range(plat + 1, len(lines)):
        if lines[i].strip() and _indent(lines[i]) <= 2:
            end = i
            break

    sig = None
    for i in range(plat + 1, end):
        if re.match(r"^    signal:\s*$", lines[i]):
            sig = i
            break

    if sig is None:
        block = _signal_block(4)
        return lines[:end] + block + lines[end:], 1

    # Existing signal block: update known keys, append missing, keep extras.
    seg_end = end
    for i in range(sig + 1, end):
        if lines[i].strip() and _indent(lines[i]) <= 4:
            seg_end = i
            break
    seen = set()
    new_seg = []
    for ln in lines[sig + 1:seg_end]:
        m = re.match(r"^(\s+)([A-Za-z_]+):", ln)
        if m and m.group(2) in SIGNAL_KEYS:
            key = m.group(2)
            seen.add(key)
            want = f"{m.group(1)}{key}: {SIGNAL_KEYS[key]}"
            if want != ln:
                changes += 1
            new_seg.append(want)
        else:
            new_seg.append(ln)
    child = " " * (_indent(lines[sig]) + 2)
    for k, v in SIGNAL_KEYS.items():
        if k not in seen:
            new_seg.append(f"{child}{k}: {v}")
            changes += 1
    return lines[:sig + 1] + new_seg + lines[seg_end:], changes


def apply(path: Path, interval: int) -> int:
    if not path.exists():
        return -1
    lines = path.read_text().splitlines()
    lines, c1 = _set_interval(lines, interval)
    lines, c2 = _apply_signal(lines)
    changes = c1 + c2
    if changes:
        path.write_text("\n".join(lines) + "\n")
    return changes


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", required=True)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = {}
    for c in args.config:
        res[c] = apply(Path(c).expanduser(), args.interval)
    total = sum(v for v in res.values() if v > 0)
    if args.json:
        print(json.dumps({"changes": res, "total": total}, indent=1))
    else:
        for c, n in res.items():
            print(f"display_config_apply: {c}: "
                  f"{'missing' if n < 0 else str(n) + ' change(s)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
