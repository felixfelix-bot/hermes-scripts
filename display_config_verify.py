#!/usr/bin/env python3
"""display_config_verify.py — deploy-time guarantee (D-139).

Exits non-zero unless the Signal platform is QUIET: its heartbeat keys
(`long_running_notifications`, `interim_assistant_messages`, `busy_ack_detail`)
must all be `false`. Role 29 runs this after `display_config_apply.py`; a deploy
that would re-enable the "⏳ Working — N min" spam fails loudly instead of
shipping. Also safe to run manually.

Usage:
  display_config_verify.py --config PATH [--config PATH ...]
Exit: 0 quiet, 1 noisy/missing/parse-error (fail-closed), 2 usage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

QUIET_KEYS = ("long_running_notifications", "interim_assistant_messages",
              "busy_ack_detail")


def _is_false(v) -> bool:
    if isinstance(v, bool):
        return v is False
    return str(v).strip().lower() in {"false", "0", "no", "off"}


def check(path: Path) -> list[str]:
    """Return a list of problems (empty = quiet)."""
    try:
        import yaml
    except Exception as exc:  # pragma: no cover
        return [f"pyyaml unavailable: {exc}"]
    if not path.exists():
        return [f"{path}: missing"]
    try:
        d = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        return [f"{path}: parse error: {exc}"]
    sig = ((d.get("display") or {}).get("platforms") or {}).get("signal")
    if not isinstance(sig, dict):
        return [f"{path}: display.platforms.signal is not a quiet mapping"]
    problems = [f"{path}: signal.{k}={sig.get(k)!r} (must be false)"
                for k in QUIET_KEYS if not _is_false(sig.get(k))]
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", required=True)
    args = ap.parse_args(argv)
    problems: list[str] = []
    for c in args.config:
        problems += check(Path(c).expanduser())
    if problems:
        print("display_config_verify: FAIL — Signal is NOT quiet:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"display_config_verify: OK — Signal quiet on {len(args.config)} config(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
