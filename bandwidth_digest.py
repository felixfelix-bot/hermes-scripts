#!/usr/bin/env python3
"""bandwidth_digest.py — daily digest + budget guard for the metered gateway.

Aggregates the per-host ``bandwidth.db`` from the local node and the remote
DQ05, rolls the current cycle up against the 350 GB budget, posts a digest to
the operator channel (Buzz) and, when signal-cli is available, to a Signal
group. At ``pause_pct`` it creates ``~/.hermes/bot/bandwidth_pause`` and (if
configured) stops ``pause_services``; the flag clears automatically when the
cycle resets.

Usage:
  bandwidth_digest.py [--dry-run] [--json] [--no-deliver]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bandwidth_meter as bm  # noqa: E402


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def merge_reports(reports: list[dict]) -> dict:
    """Combine per-host report dicts into one rollup."""
    by_key: dict[tuple, dict] = {}
    for rep in reports:
        for row in rep.get("rows", []):
            key = (row["scope"], row["name"])
            acc = by_key.setdefault(key, {"scope": row["scope"], "name": row["name"],
                                          "rx": 0, "tx": 0})
            acc["rx"] += row.get("rx", 0)
            acc["tx"] += row.get("tx", 0)
    rows = sorted(by_key.values(), key=lambda r: r["rx"] + r["tx"], reverse=True)
    return {"total_bytes": sum(r["rx"] + r["tx"] for r in rows), "rows": rows}


def format_digest(merged: dict, budget_gb: float, level: str) -> str:
    total = merged["total_bytes"]
    pct = (total / (budget_gb * 1024 ** 3) * 100) if budget_gb else 0
    lines = [
        f"Bandwidth cycle digest — {bm.format_bytes(total)} / {budget_gb} GB "
        f"({pct:.0f}%) [{level}]",
    ]
    for r in merged["rows"][:12]:
        lines.append(f"  {r['scope']:9} {r['name']:34} "
                     f"rx {bm.format_bytes(r['rx'])} tx {bm.format_bytes(r['tx'])}")
    return "\n".join(lines)


def remote_report(ssh_target: str, cycle_day: int) -> dict | None:
    """Fetch the remote host's meter report as JSON over ssh."""
    cmd = (f'HERMES_HOME=$HOME/.hermes python3 '
           f'$HOME/.hermes/scripts/bandwidth_meter.py --report --json '
           f'--config $HOME/.hermes/bot/bandwidth.json 2>/dev/null')
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                              ssh_target, cmd],
                             capture_output=True, text=True, timeout=30).stdout
        return json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except Exception:
        return None


def deliver(text: str, cfg: dict) -> list[str]:
    used: list[str] = []
    # Buzz / operator channel
    alert = hermes_home() / "scripts" / "operator_alert.py"
    if alert.exists():
        try:
            subprocess.run([sys.executable, str(alert), "--topic", "bandwidth",
                            "--text", text, "--cooldown", "0"],
                           capture_output=True, timeout=30)
            used.append("operator-alerts")
        except Exception:
            pass
    # Signal (best-effort; signal-cli may be unavailable)
    group = cfg.get("signal_group", "")
    if group:
        try:
            subprocess.run(["signal-cli", "send", "-g", group, "-m", text],
                           capture_output=True, timeout=30)
            used.append("signal")
        except Exception:
            pass
    return used


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(hermes_home() / "bot" / "bandwidth.json"))
    ap.add_argument("--db", default=str(hermes_home() / "state" / "bandwidth.db"))
    ap.add_argument("--remote", default="dq05", help="ssh target for the other metered node")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-deliver", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = bm._load_config(Path(args.config))
    cycle_day = cfg.get("cycle_day", 1)
    budget = cfg.get("budget_gb", 350)

    local = bm.report(Path(args.db), cycle_day)
    remote = remote_report(args.remote, cycle_day)
    merged = merge_reports([local] + ([remote] if remote else []))
    level = bm.guard_level(merged["total_bytes"], budget,
                           cfg.get("warn_pct", 70), cfg.get("pause_pct", 80))

    pause_flag = hermes_home() / "bot" / "bandwidth_pause"
    if level == "pause":
        pause_flag.parent.mkdir(parents=True, exist_ok=True)
        if not args.dry_run:
            pause_flag.write_text(str(time.time()))
    elif pause_flag.exists() and not args.dry_run:
        pause_flag.unlink()

    text = format_digest(merged, budget, level)
    delivered = [] if (args.dry_run or args.no_deliver) else deliver(text, cfg)

    if args.json:
        print(json.dumps({**merged, "level": level, "budget_gb": budget,
                          "delivered": delivered, "remote_ok": remote is not None}))
    else:
        print(text)
        if delivered:
            print(f"delivered: {', '.join(delivered)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
