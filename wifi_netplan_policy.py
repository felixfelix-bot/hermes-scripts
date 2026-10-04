#!/usr/bin/env python3
"""wifi_netplan_policy.py — keep WiFi autoconnect policy in NM *and* netplan.

Problem (t_e7c5d5534f00): a roaming laptop auto-joined open/OWE hotspots
(TollGate-*, tg-*, freifunk portals) that do not deliver internet. NetworkManager
picks the highest-priority *autoconnect* profile, but every profile here had
priority 0, so once NM landed on a dead open AP it never came back to the
trusted private network — even after that network reappeared.

Fix: classify every WiFi connection as
  * trusted  -> connection.autoconnect=yes, autoconnect-priority=<trusted> (100)
  * open/bad -> connection.autoconnect=yes, autoconnect-priority=<open>   (-100)
  * other    -> untouched
so NM prefers (and fails back to) trusted networks and treats the open hotspots
as a last resort.

Persistence: on netplan-managed Ubuntu these connections are generated from
/etc/netplan/90-NM-<uuid>.yaml, so an `nmcli connection modify` alone is not
guaranteed to survive a regenerate. This helper therefore also writes the two
keys into the connection's `networkmanager.passthrough` map in the backing
netplan file. It never runs `netplan apply` (that can bounce the live link over
SSH); callers pass --reload to run the safe `netplan generate` + `nmcli
connection reload`.

Exit 0 on success. `--apply` writes; otherwise it is a dry run that reports the
changes it *would* make.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - target may lack PyYAML
    yaml = None

AUTOCONNECT = "connection.autoconnect"
PRIORITY = "connection.autoconnect-priority"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def _nmcli(args: list[str]) -> subprocess.CompletedProcess:
    """Run nmcli, prefixing sudo -n when not root (polkit otherwise prompts)."""
    cmd = ["nmcli", *args]
    if os.geteuid() != 0 and shutil.which("sudo"):
        cmd = ["sudo", "-n", *cmd]
    return _run(cmd)


def active_wifi_connections() -> list[dict]:
    """Every WiFi connection: {name, uuid, ssid, autoconnect, priority}."""
    r = _nmcli(["-t", "-f", "NAME,TYPE,UUID", "connection", "show"])
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) < 3 or parts[1] != "802-11-wireless":
            continue
        name, uuid = parts[0], parts[2]
        ssid = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", uuid]).stdout.strip()
        g = _nmcli(["-g", f"{AUTOCONNECT},{PRIORITY}", "connection", "show", uuid]).stdout.strip()
        bits = (g.splitlines() or [""])[0].split(":") if g else []
        out.append({
            "name": name,
            "uuid": uuid,
            "ssid": ssid,
            "autoconnect": (bits[0] if len(bits) > 0 else "").strip(),
            "priority": (bits[1] if len(bits) > 1 else "").strip(),
        })
    return out


def classify(ssid: str, trusted: list[str], open_pats: list[str]) -> str:
    import fnmatch
    for pat in open_pats:
        if fnmatch.fnmatch(ssid, pat):
            return "open"
    for pat in trusted:
        if fnmatch.fnmatch(ssid, pat):
            return "trusted"
    return "other"


def _netplan_file_for(uuid: str, netplan_dir: Path) -> Path | None:
    direct = netplan_dir / f"90-NM-{uuid}.yaml"
    if direct.is_file():
        return direct
    if yaml is None:
        return None
    for f in sorted(netplan_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        wifis = ((doc.get("network") or {}).get("wifis") or {})
        for w in wifis.values():
            if ((w or {}).get("networkmanager") or {}).get("uuid") == uuid:
                return f
    return None


def _ap_key(wifi: dict, uuid: str, ssid: str) -> str | None:
    aps = wifi.get("access-points") or {}
    for key, ap in aps.items():
        nm = (ap or {}).get("networkmanager") or {}
        if nm.get("uuid") == uuid:
            return key
    if ssid in aps:
        return ssid
    if len(aps) == 1:
        return next(iter(aps))
    return None


def patch_netplan(path: Path, uuid: str, ssid: str,
                  autoconnect: bool, priority: int) -> bool:
    """Set the two passthrough keys under the matching AP. Returns True if the
    file content changed. Raises on YAML we cannot safely edit (caller skips)."""
    if yaml is None:
        raise RuntimeError("PyYAML not installed")
    original = path.read_text()
    doc = yaml.safe_load(original) or {}
    wifis = ((doc.get("network") or {}).get("wifis") or {})
    for w in wifis.values():
        if ((w or {}).get("networkmanager") or {}).get("uuid") != uuid:
            continue
        key = _ap_key(w or {}, uuid, ssid)
        if key is None:
            raise RuntimeError(f"AP {ssid!r} not found under uuid {uuid} in {path}")
        ap = w["access-points"][key]
        nm = ap.setdefault("networkmanager", {})
        pt = nm.setdefault("passthrough", {})
        pt[AUTOCONNECT] = "true" if autoconnect else "false"
        pt[PRIORITY] = str(int(priority))
        new = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False,
                             allow_unicode=True)
        if new == original:
            return False
        shutil.copy2(path, str(path) + ".bak-wifi-policy")
        path.write_text(new)
        return True
    raise RuntimeError(f"uuid {uuid} not found in {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trusted", action="append", default=[],
                    help="glob of a trusted SSID (repeatable)")
    ap.add_argument("--open", dest="open_pats", action="append", default=[],
                    help="glob of an open/no-internet SSID (repeatable)")
    ap.add_argument("--from-config", default=None,
                    help="read trusted/open patterns + priorities from this "
                         "wifi_uplink_guard.json (the role passes the rendered "
                         "config so policy and watchdog never diverge)")
    ap.add_argument("--trusted-priority", type=int, default=100)
    ap.add_argument("--open-priority", type=int, default=-100)
    ap.add_argument("--netplan-dir", default="/etc/netplan")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--reload", action="store_true",
                    help="after applying: netplan generate + nmcli connection reload")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.from_config:
        try:
            cfg = json.loads(Path(args.from_config).read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot read --from-config {args.from_config}: {exc}",
                  file=sys.stderr)
            return 2
        args.trusted = (list(args.trusted) + list(cfg.get("trusted_ssids", []))
                        + list(cfg.get("trusted_patterns", [])))
        args.open_pats = list(args.open_pats) + list(cfg.get("open_patterns", []))
        if "trusted_priority" in cfg:
            args.trusted_priority = int(cfg["trusted_priority"])
        if "open_priority" in cfg:
            args.open_priority = int(cfg["open_priority"])

    netplan_dir = Path(args.netplan_dir)
    report = []
    for conn in active_wifi_connections():
        klass = classify(conn["ssid"], args.trusted, args.open_pats)
        if klass == "other":
            continue
        want_auto = "yes"
        want_prio = args.trusted_priority if klass == "trusted" else args.open_priority
        nm_changed = (conn["autoconnect"] != want_auto
                      or conn["priority"] != str(want_prio))
        entry = {"name": conn["name"], "uuid": conn["uuid"], "ssid": conn["ssid"],
                 "class": klass, "nm_changed": nm_changed, "netplan_changed": False,
                 "netplan_file": None, "error": None}

        if args.apply and nm_changed:
            r = _nmcli(["connection", "modify", conn["uuid"],
                        AUTOCONNECT, want_auto, PRIORITY, str(want_prio)])
            if r.returncode != 0:
                entry["error"] = (r.stderr or r.stdout).strip()

        if args.apply:
            nf = _netplan_file_for(conn["uuid"], netplan_dir)
            entry["netplan_file"] = str(nf) if nf else None
            if nf:
                try:
                    entry["netplan_changed"] = patch_netplan(
                        nf, conn["uuid"], conn["ssid"],
                        autoconnect=True, priority=want_prio)
                except Exception as exc:  # never clobber: report, skip
                    entry["error"] = f"netplan: {exc}"
        report.append(entry)

    if args.apply and args.reload:
        _run(["sudo", "-n", "netplan", "generate"]) if os.geteuid() != 0 \
            else _run(["netplan", "generate"])
        _nmcli(["connection", "reload"])

    if args.json:
        print(json.dumps({"apply": args.apply, "connections": report}, indent=1))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        for e in report:
            print(f"[{mode}] {e['class']:7} {e['ssid'] or e['name']}: "
                  f"nm_changed={e['nm_changed']} netplan_changed={e['netplan_changed']}"
                  + (f" ERROR={e['error']}" if e["error"] else ""))
        if not report:
            print(f"[{mode}] no trusted/open WiFi connections matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
