#!/usr/bin/env python3
"""wifi_profiles_apply.py — render shared WiFi profiles to netplan (idempotent).

Reads ``{"profiles": [...]}`` (or a bare array) on **stdin** and writes one
netplan file per secured profile at ``<netplan-dir>/90-NM-<uuid5>.yaml`` with a
DETERMINISTIC uuid, so repeated converges across laptops are idempotent. The
passwords come from the in-memory stdin payload (delivered from OpenBao by
fleet_secret.py) and are written only into the root-owned netplan file — never
into the repo, a temp file, or a log.

Layout mirrors the existing NM-managed files: renderer NetworkManager, dhcp4+6,
and a ``passthrough`` that sets the trusted autoconnect priority. Profiles whose
file is already byte-identical are skipped. ``--reload`` runs the safe
``netplan generate`` + ``nmcli connection reload`` — never ``netplan apply``,
which can bounce the live link (and an SSH session) out from under us.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

# Fixed namespace so the same SSID+key-mgmt yields the same connection uuid on
# every laptop (needed for idempotency and for netplan file naming).
_NAMESPACE = uuid.UUID("6f2f0f0e-9c2b-4b2a-9c2b-6f2f0f0e9c2b")


def _uuid_for(ssid: str, key_mgmt: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"hermes-wifi:{ssid}:{key_mgmt}"))


def render(profile: dict, priority: int) -> str:
    ssid = profile["ssid"]
    key_mgmt = (profile.get("key_mgmt") or "none").strip()
    psk = (profile.get("psk") or "").strip()
    conn_uuid = _uuid_for(ssid, key_mgmt)
    ap: dict = {
        "networkmanager": {
            "uuid": conn_uuid,
            "name": ssid,
            "passthrough": {
                "connection.autoconnect": "true",
                "connection.autoconnect-priority": str(int(priority)),
            },
        },
    }
    if psk:
        ap["auth"] = {"key-management": key_mgmt, "password": psk}
    doc = {"network": {"version": 2, "wifis": {
        f"NM-{conn_uuid}": {
            "renderer": "NetworkManager",
            "dhcp4": True,
            "dhcp6": True,
            "access-points": {ssid: ap},
            "networkmanager": {"uuid": conn_uuid, "name": ssid},
        },
    }}}
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False,
                          allow_unicode=True)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    cmd = list(args)
    if os.geteuid() != 0 and shutil.which("sudo"):
        cmd = ["sudo", "-n", *cmd]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netplan-dir", default="/etc/netplan")
    ap.add_argument("--trusted-priority", type=int, default=100)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("wifi_profiles_apply: empty stdin (no profiles) — nothing to do",
              file=sys.stderr)
        return 0
    data = json.loads(raw)
    profiles = data.get("profiles", data) if isinstance(data, dict) else data
    if not isinstance(profiles, list):
        print("wifi_profiles_apply: stdin is not a profile list", file=sys.stderr)
        return 2

    netplan_dir = Path(args.netplan_dir)
    changed, results = [], []
    for p in profiles:
        if not p.get("ssid"):
            continue
        conn_uuid = _uuid_for(p["ssid"], (p.get("key_mgmt") or "none").strip())
        path = netplan_dir / f"90-NM-{conn_uuid}.yaml"
        content = render(p, args.trusted_priority)
        status = "unchanged"
        if not path.exists() or path.read_text() != content:
            status = "written"
            if not args.dry_run:
                netplan_dir.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    shutil.copy2(path, str(path) + ".bak-wifi-profiles")
                path.write_text(content)
                os.chmod(path, 0o600)
            changed.append(p["ssid"])
        results.append({"ssid": p["ssid"], "uuid": conn_uuid,
                        "path": str(path), "status": status})

    if changed and args.reload and not args.dry_run:
        _run(["netplan", "generate"])
        _run(["nmcli", "connection", "reload"])

    if args.json:
        print(json.dumps({"changed": changed, "profiles": results}, indent=1))
    else:
        for r in results:
            print(f"[{r['status']:9}] {r['ssid']} -> {r['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
