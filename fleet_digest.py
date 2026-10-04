#!/usr/bin/env python3
"""fleet_digest.py — postmortem / efficiency retro digest (H.16).

Summarizes the last N hours of fleet interventions, freeze/quarantine state,
and the efficiency trend into ``~/.hermes/logs/fleet-digest.md`` and (with
``--push``) a compact signed ``fleet-digest`` event on buzz.

Usage:
  fleet_digest.py [--hours 24] [--push] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
LEDGER = BOT / "fleet_interventions.jsonl"
HEALTH_HIST = BOT / "fleet_health_history.json"
BLEED_STATE = BOT / "bleed_guard_state.json"
OUT = HERMES / "logs" / "fleet-digest.md"
OPS_CFG = BOT / "hermes_ops.json"


def _read_json(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _publish(content: str) -> bool:
    ops = _read_json(OPS_CFG, {})
    group = ops.get("orange_group")
    nsec = Path(os.path.expanduser(ops.get("node_nsec", "")))
    if not group or not nsec.exists():
        return False
    try:
        key = nsec.read_text().strip()
    except OSError:
        return False
    try:
        r = subprocess.run(
            [_nak(), "event", "-k", "9", "-c", content, "-t", f"h={group}",
             "-t", "client=hermes-fleet", "-t", "t=fleet-digest", "--sec", key,
             "--auth", ops.get("orange_relay", "wss://relay.orangesync.tech")],
            capture_output=True, text=True, timeout=45)
        return "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def _node_balance() -> list[str]:
    """D-130: per-node load/memory/worker headroom so imbalance is visible."""
    nodes = []
    hb = _read_json(BOT / "fleet_heartbeat.json", {})
    if hb:
        nodes.append(hb)
    for f in sorted((BOT / "peers").glob("*.json")) if (BOT / "peers").exists() else []:
        d = _read_json(f, None)
        if isinstance(d, dict) and d.get("ts") and \
                ("load1_per_cpu" in d or "nproc" in d):
            nodes.append(d)
    best: dict[str, dict] = {}
    for d in nodes:
        n = d.get("node")
        if n and (n not in best or d.get("ts", 0) >= best[n].get("ts", 0)):
            best[n] = d
    out = []
    for n, d in sorted(best.items()):
        cap = d.get("smoothed_cap") or (d.get("pressure") or {}).get("smoothed_cap")
        out.append(f"- {n}: load/cpu {d.get('load1_per_cpu')} · "
                   f"mem_free {d.get('mem_available_mb')}MB · "
                   f"workers {d.get('hermes_workers')} · cap {cap}")
    return out


def _balance_alert(threshold_pct: float = 25.0,
                   min_mem_mb: int = 1500) -> list[str]:
    """D-131 10.9: warn on sustained cross-node imbalance or memory pressure."""
    best: dict[str, dict] = {}
    nodes = []
    hb = _read_json(BOT / "fleet_heartbeat.json", {})
    if hb:
        nodes.append(hb)
    for f in sorted((BOT / "peers").glob("*.json")) if (BOT / "peers").exists() else []:
        d = _read_json(f, None)
        if isinstance(d, dict) and d.get("ts") and \
                ("load1_per_cpu" in d or "nproc" in d):
            nodes.append(d)
    for d in nodes:
        n = d.get("node")
        if n and (n not in best or d.get("ts", 0) >= best[n].get("ts", 0)):
            best[n] = d
    out: list[str] = []
    loads = [float(d["load1_per_cpu"]) for d in best.values()
             if d.get("load1_per_cpu") is not None]
    if len(loads) >= 2:
        mx, mn = max(loads), min(loads)
        if mx >= 1.0 and (mx - mn) / mx * 100.0 > threshold_pct:
            out.append(f"WARNING: load/cpu imbalance {mn:.2f}–{mx:.2f} "
                       f"({(mx - mn) / mx * 100:.0f}% > {threshold_pct:.0f}%)")
    for n, d in sorted(best.items()):
        mem = d.get("mem_available_mb")
        if isinstance(mem, (int, float)) and mem < min_mem_mb:
            out.append(f"WARNING: {n} low memory — {int(mem)} MB free (< {min_mem_mb})")
    return out


def build(hours: int) -> str:
    now = time.time()
    cutoff = now - hours * 3600
    actions = Counter()
    by_target = Counter()
    n = 0
    try:
        for line in LEDGER.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if float(e.get("ts", 0)) < cutoff:
                continue
            n += 1
            actions[e.get("action", "?")] += 1
            by_target[e.get("target", "?")] += 1
    except OSError:
        pass

    bleed = _read_json(BLEED_STATE, {})
    hist = _read_json(HEALTH_HIST, {"buckets": []}).get("buckets", [])[-hours:]

    lines = [
        f"# Fleet digest — last {hours}h",
        f"_generated {datetime.now(timezone.utc).isoformat()}_",
        "",
        f"- interventions logged: **{n}**",
        f"- by action: {dict(actions)}",
        f"- by target: {dict(by_target)}",
        f"- frozen: {bleed.get('frozen')}  quarantined: {bleed.get('quarantined')}  "
        f"storm_cycles(state): {len(bleed.get('storm_cycles') or [])}",
        "",
        "## Efficiency trend (hourly spend / units)",
    ]
    for b in hist:
        lines.append(f"- h{b.get('hour')}: ${b.get('spend')} / {b.get('units')} units "
                     f"= ${b.get('cost_per_unit')}/unit")
    if not hist:
        lines.append("- (no history yet)")
    lines += ["", "## Fleet balance (D-130)"]
    lines += _node_balance() or ["- (no heartbeats)"]
    alerts = _balance_alert()
    if alerts:
        lines += ["", "## Imbalance alerts (D-131)"] + alerts
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    md = build(args.hours)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(md)
    if args.push:
        summary = "fleet-digest: " + " | ".join(
            ln for ln in md.splitlines() if ln.startswith("- interventions")
            or ln.startswith("- frozen") or ln.startswith("- by action"))
        _publish(summary)
    if args.json:
        print(json.dumps({"out": str(OUT), "bytes": len(md)}))
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
