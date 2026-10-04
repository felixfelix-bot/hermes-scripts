#!/usr/bin/env python3
"""digest_plots.py — attach the configured visualization PNGs to operator digests.

Reads ``~/.hermes/bot/digest_plots.json`` and, for the requested cadence
(``daily``/``weekly``), sends each plot via ``send-viz-signal.sh``. Keeps the
"what gets shared, how often" decision in config-as-code instead of hardcoded in
the digest script.

Config shape:
  {"version": 1,
   "daily":  ["model-mix-7d", "price-bubbles"],
   "weekly": ["price-bubbles"],
   "messages": {"model-mix-7d": "…", "price-bubbles": "…"}}

Usage:
  digest_plots.py daily [--dry-run] [--viz-dir DIR] [--sender PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DEFAULT_CFG = HERMES / "bot" / "digest_plots.json"
DEFAULT_VIZ = HERMES / "viz"
DEFAULT_SENDER = HERMES / "bot" / "scripts" / "send-viz-signal.sh"

FALLBACK_CFG = {"version": 1, "daily": ["model-mix-7d", "price-bubbles"],
                "weekly": ["price-bubbles"], "messages": {}}


def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CFG
    try:
        d = json.loads(p.read_text())
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return dict(FALLBACK_CFG)


def plots_for(cadence: str, cfg: dict) -> list[str]:
    v = cfg.get(cadence)
    return [str(x) for x in v] if isinstance(v, list) else []


def send(plot: str, message: str, sender: Path, viz_dir: Path,
         dry: bool = False) -> bool:
    png = viz_dir / f"{plot}.png"
    if not png.exists():
        print(f"digest_plots: skip {plot} (no {png})", file=sys.stderr)
        return False
    cmd = ["bash", str(sender), "--plot", plot]
    if message:
        cmd += ["--message", message]
    if dry:
        print("DRY:", " ".join(cmd))
        return True
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"digest_plots: send {plot} failed: {e}", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cadence", choices=["daily", "weekly"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--viz-dir", default=None)
    ap.add_argument("--sender", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    viz = Path(os.path.expanduser(args.viz_dir)) if args.viz_dir else DEFAULT_VIZ
    sender = Path(os.path.expanduser(args.sender)) if args.sender else DEFAULT_SENDER
    msgs = cfg.get("messages") or {}
    sent = 0
    for plot in plots_for(args.cadence, cfg):
        if send(plot, msgs.get(plot, ""), sender, viz, dry=args.dry_run):
            sent += 1
    print(f"digest_plots: {args.cadence}: sent {sent}/{len(plots_for(args.cadence, cfg))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
