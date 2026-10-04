#!/usr/bin/env python3
"""map_drift.py — surface fleet-map drift; propose allowlist changes (Phase W2.3).

Advisory only — never edits policy. Compares the operator maps
(`state/fleet/{public_boards,private_offload_boards,local_only_boards}.json`)
against the classifier's evidence and reports:

  * additions   — proven-public boards missing from the allowlist
  * removals    — allowlisted boards no longer proven public
  * local_mismatch — local_only/opt-in boards that look offloadable (and vice-versa)

`--notify` posts the report to the Buzz operator-decisions channel and, if
`MAP_DRIFT_SIGNAL_TARGET` is set, sends a Signal nudge referencing Buzz.

Usage:
  map_drift.py --evidence-dir ~/.hermes/bot/fleet_map --json
  map_drift.py --notify
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
# Deployed maps live in bot/fleet_map (role 18). Fall back to the repo state dir
# when running straight from a checkout.
STATE = HERMES / "bot" / "fleet_map"
if not (STATE / "public_boards.json").exists() and (ROOT / "state" / "fleet" / "public_boards.json").exists():
    STATE = ROOT / "state" / "fleet"
DECISIONS = HERMES / "bot" / "decisions_channel.json"


def _keys(path: Path) -> set[str]:
    try:
        d = json.loads(path.read_text())
    except Exception:
        return set()
    if isinstance(d, dict) and isinstance(d.get("boards"), list):
        return {str(b) for b in d["boards"]}
    if isinstance(d, dict):
        return set(d.keys())
    return set()


def load_maps(state: Path = STATE) -> dict:
    return {
        "public": _keys(state / "public_boards.json"),
        "optin": _keys(state / "private_offload_boards.json"),
        "local": _keys(state / "local_only_boards.json"),
    }


def proven_public(evidence_dir: Path) -> set[str]:
    """Boards the classifier proved public (its public_boards.json)."""
    return _keys(Path(evidence_dir) / "public_boards.json")


def build_report(maps: dict, proven: set[str]) -> dict:
    allow, optin, local = maps["public"], maps["optin"], maps["local"]
    additions = sorted((proven - allow) - local)
    removals = sorted(b for b in allow if proven and b not in proven)
    local_mismatch = sorted((local & (proven | optin)))
    optin_missing = sorted(b for b in optin if proven and b in proven)
    return {
        "propose_public_additions": additions,
        "propose_public_removals": removals,
        "local_only_but_offloadable": local_mismatch,
        "optin_boards_now_public": optin_missing,
        "counts": {"public": len(allow), "optin": len(optin), "local": len(local),
                   "proven_public": len(proven)},
    }


def format_report(rep: dict) -> str:
    c = rep["counts"]
    lines = [f"[map-drift] public={c['public']} optin={c['optin']} local={c['local']} "
             f"proven_public={c['proven_public']}"]
    for k, label in (("propose_public_additions", "propose ADD to public allowlist"),
                     ("propose_public_removals", "propose REMOVE from allowlist"),
                     ("local_only_but_offloadable", "local_only but offloadable"),
                     ("optin_boards_now_public", "opt-in boards now public")):
        if rep[k]:
            lines.append(f"  {label}: {', '.join(rep[k][:20])}")
    if len(lines) == 1:
        lines.append("  no drift")
    return "\n".join(lines)


def notify_buzz(text: str) -> bool:
    try:
        cfg = json.loads(DECISIONS.read_text())
        group = cfg.get("orange_group")
        relay = cfg.get("relay", "")
    except Exception:
        cfg, group, relay = {}, None, ""
    ops = {}
    try:
        ops = json.loads((HERMES / "bot" / "hermes_ops.json").read_text())
    except Exception:
        pass
    group = group or ops.get("orange_group")
    relay = relay or ops.get("orange_relay", "")
    if not group:
        return False
    nsec = Path(os.path.expanduser(ops.get("node_nsec", "")))
    nak = next((c for c in [str(Path.home() / ".local/bin/nak"), "/usr/local/bin/nak",
                            "/usr/bin/nak"] if Path(c).exists()), "nak")
    if not nsec.exists():
        return False
    try:
        key = nsec.read_text().strip()
    except OSError:
        return False
    try:
        r = subprocess.run([nak, "event", "-k", "9", "-c", text,
                            "-t", f"h={group}", "-t", "client=hermes-fleet",
                            "--sec", key, "--auth", relay],
                           capture_output=True, text=True, timeout=45)
        return "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def notify_signal(text: str) -> bool:
    target = os.environ.get("MAP_DRIFT_SIGNAL_TARGET", "")
    if not target:
        return False
    try:
        r = subprocess.run(["signal-cli", "send", "-m", text, target],
                           capture_output=True, text=True, timeout=45)
        return r.returncode == 0
    except Exception:
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fleet-map drift scan (W2.3)")
    ap.add_argument("--state-dir", default=str(STATE))
    ap.add_argument("--evidence-dir", default=str(HERMES / "bot" / "fleet_map"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args(argv)
    maps = load_maps(Path(args.state_dir))
    proven = proven_public(Path(args.evidence_dir))
    rep = build_report(maps, proven)
    text = format_report(rep)
    print(json.dumps(rep, indent=1) if args.json else text)
    if args.notify:
        posted = notify_buzz(text)
        nudged = notify_signal("Fleet map drift (see Buzz operator-decisions):\n" + text)
        print(f"[map-drift] buzz={posted} signal={nudged}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
