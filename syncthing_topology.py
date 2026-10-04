#!/usr/bin/env python3
"""Enforce the Syncthing topology: the local mesh (CobradorWave <-> DQ05) and the
VPS mesh (VPS <-> VPS) must never bridge, because a local<->VPS link exhausts
the metered home gateway.

For every Syncthing instance found on the host this:
  * deletes any device IDs in ``--forbid`` (the *other* mesh's members), and
  * (with ``--disable-internet``) turns off relays / global announce / NAT,
    so only local discovery remains.

Idempotent: re-running with the link already removed is a no-op. Config files
are never written directly; changes go through the Syncthing REST API so the
daemon stays consistent. Always back up ``config.xml`` (the role does this).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.request

DEVICE_ID_RE = re.compile(r"<device id=\"([A-Z0-9]{7}(?:-[A-Z0-9]{7}){7})\"")
APIKEY_RE = re.compile(r"<apikey>([^<]+)</apikey>")
GUI_RE = re.compile(r"<gui[^>]*>.*?<address>([^<]+)</address>", re.S)

CANDIDATE_CONFIGS = [
    "~/.local/state/syncthing/config.xml",
    "~/.config/syncthing/config.xml",
    "/var/lib/syncthing/.config/syncthing/config.xml",
    "/root/.local/state/syncthing/config.xml",
]


def discover_configs(extra: list[str] | None = None) -> list[str]:
    """Existing Syncthing config files on this host (expanded, de-duped)."""
    seen: list[str] = []
    for cand in (extra or []) + CANDIDATE_CONFIGS:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and path not in seen:
            seen.append(path)
    return seen


def parse_config(path: str) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    key = APIKEY_RE.search(text)
    gui = GUI_RE.search(text)
    return {
        "path": path,
        "apikey": key.group(1) if key else None,
        "gui": gui.group(1) if gui else "127.0.0.1:8384",
        "device_ids": sorted(set(DEVICE_ID_RE.findall(text))),
    }


def select_forbidden(present: list[str], forbidden: list[str]) -> list[str]:
    """Forbidden device IDs actually present, preserving the forbidden order."""
    have = set(present)
    return [d for d in forbidden if d in have]


def _api(base: str, key: str, method: str, path: str, body: dict | None = None) -> int:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _get_options(base: str, key: str) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + "/rest/config/options",
        headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def isolate(cfg: dict, forbidden: list[str], disable_internet: bool,
            dry_run: bool = False) -> list[tuple]:
    """Apply the topology to one instance; return the actions taken/planned."""
    actions: list[tuple] = []
    if not cfg.get("apikey"):
        return [("skip", "no apikey")]

    base = "http://" + cfg["gui"]
    for did in select_forbidden(cfg["device_ids"], forbidden):
        actions.append(("delete-device", did))
        if not dry_run:
            _api(base, cfg["apikey"], "DELETE", f"/rest/config/devices/{did}")

    if disable_internet:
        want = {"relaysEnabled": False, "globalAnnounceEnabled": False,
                "natEnabled": False}
        if dry_run:
            need = True
        else:
            cur = _get_options(base, cfg["apikey"])
            need = any(cur.get(k) is not False for k in want)
        if need:
            actions.append(("disable-internet", want))
            if not dry_run:
                _api(base, cfg["apikey"], "PATCH", "/rest/config/options", want)
    return actions


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--forbid", action="append", default=[],
                    help="device ID that must not be present (repeatable)")
    ap.add_argument("--disable-internet", action="store_true",
                    help="disable relays / global announce / NAT")
    ap.add_argument("--config", action="append", default=[],
                    help="extra config path to check (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    results = []
    for path in discover_configs(args.config):
        try:
            cfg = parse_config(path)
        except OSError as exc:
            results.append({"config": path, "gui": None,
                            "forbidden_present": [],
                            "actions": [("skip", f"unreadable: {exc}")]})
            continue
        acts = isolate(cfg, args.forbid, args.disable_internet, args.dry_run)
        results.append({"config": path, "gui": cfg["gui"],
                        "forbidden_present": select_forbidden(cfg["device_ids"], args.forbid),
                        "actions": acts})

    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"{r['config']} ({r['gui']}):")
            if not r["actions"]:
                print("  already isolated")
            for kind, detail in r["actions"]:
                print(f"  {kind}: {detail}")
        if not results:
            print("no syncthing configs found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
