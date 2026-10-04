#!/usr/bin/env python3
"""wifi_profiles_export.py — export secured WiFi profiles for OpenBao seeding.

Reads the root-only netplan backing files (``/etc/netplan/90-NM-*.yaml``) and
prints the ``{ssid, key_mgmt, psk, band, channel}`` set as JSON on stdout. The
operator pipes it into OpenBao once::

    sudo python3 wifi_profiles_export.py \
      | bao kv put secret/fleet/wifi-profiles profiles=@-

Nothing is written to disk; the repo never sees a PSK. The role then delivers
the set in-memory via ``fleet_secret.py`` and renders one deterministic netplan
connection per profile on each laptop.

By default only password-bearing profiles are exported (open SSIDs carry no
secret and are covered by the policy in wifi_netplan_policy.py). Use
``--include-open`` to include them (psk omitted) and ``--exclude`` globs to drop
SSIDs you do not want replicated.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

import yaml


def _load(path: Path):
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def collect(netplan_dir: Path, include_open: bool, excludes: list[str]) -> list[dict]:
    seen: dict[str, dict] = {}
    for f in sorted(netplan_dir.glob("*.yaml")):
        doc = _load(f)
        wifis = ((doc.get("network") or {}).get("wifis") or {})
        for w in wifis.values():
            for ssid, ap in (w.get("access-points") or {}).items():
                if any(fnmatch.fnmatch(ssid, pat) for pat in excludes):
                    continue
                auth = (ap or {}).get("auth") or {}
                key_mgmt = auth.get("key-management")
                psk = auth.get("password")
                if not psk and not include_open:
                    continue
                if not key_mgmt and not psk:
                    continue  # truly open and not requested
                entry = {
                    "ssid": ssid,
                    "key_mgmt": key_mgmt or "none",
                    "psk": psk or "",
                    "band": ap.get("band"),
                    "channel": ap.get("channel"),
                }
                # Prefer a profile that actually carries a secret.
                if ssid not in seen or (psk and not seen[ssid]["psk"]):
                    seen[ssid] = entry
    return [seen[k] for k in sorted(seen)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netplan-dir", default="/etc/netplan")
    ap.add_argument("--include-open", action="store_true")
    ap.add_argument("--exclude", action="append", default=[],
                    help="glob of an SSID to exclude (repeatable)")
    ap.add_argument("--wrap", action="store_true",
                    help='emit {"profiles": [...]} instead of the bare array')
    args = ap.parse_args()

    profiles = collect(Path(args.netplan_dir), args.include_open, args.exclude)
    out = {"profiles": profiles} if args.wrap else profiles
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    print(f"# {len(profiles)} profile(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
