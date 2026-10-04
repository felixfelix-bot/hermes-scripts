#!/usr/bin/env python3
"""wifi_uplink_guard.py — edge-triggered WiFi failback watchdog (t_e7c5d5534f00).

Companion to ``wifi_netplan_policy.py``. The policy makes NetworkManager
*prefer* trusted networks and *deprioritise* open hotspots; this watchdog is the
belt-and-suspenders guarantee that the box actually comes back when the
preferred gateway returns, even if NM does not roam on its own.

Each tick:
  1. find the active WiFi connection;
  2. probe real internet (TLS to the provider set — no polkit, no ping);
  3. if offline and a *trusted* SSID is visible, bring that profile up and
     re-probe (try candidates in signal order);
  4. if the box is parked on an open/no-internet hotspot, this is exactly the
     case to escape; if already on trusted but that gateway is down, do NOT
     switch to an open hotspot — alert instead;
  5. emit an operator alert ONLY on a state change (edge-triggered).

Output is the alert payload; the script exits 0 whenever it ran (a condition is
not a job failure — a broken script is). Same convention as router-watchdog.sh.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
CONFIG = HERMES / "bot" / "wifi_uplink_guard.json"

DEFAULTS = {
    "enabled": True,
    "trusted_ssids": [],
    "trusted_patterns": ["EnterSSID-*"],
    "open_patterns": ["TollGate-*", "tg-*", "tollgate-*", "berlin.freifunk.net",
                      "BER-Portal", "FOSDEM", "_Free_Wifi_Berlin", "Geekz.open"],
    "providers": ["api.deepseek.com", "api.deepinfra.com"],
    "tls_timeout_s": 5,
    "settle_s": 6,
    "state_file": str(HERMES / "bot" / ".wifi-uplink-guard.state"),
    "alert_cmd": "",
    "alert_cooldown_s": 3600,
}


def log(*p):
    print(f"[wifi-uplink-guard] {datetime.now(timezone.utc).isoformat()}", *p,
          file=sys.stderr, flush=True)


def _sudo_nmcli(args: list[str]) -> subprocess.CompletedProcess:
    cmd = ["nmcli", *args]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except Exception:
        pass
    return cfg


def classify(ssid: str, cfg: dict) -> str:
    for pat in cfg["open_patterns"]:
        if fnmatch.fnmatch(ssid, pat):
            return "open"
    for pat in list(cfg["trusted_ssids"]) + list(cfg["trusted_patterns"]):
        if fnmatch.fnmatch(ssid, pat) or ssid == pat:
            return "trusted"
    return "other"


def active_wifi() -> tuple[str | None, str | None, str | None]:
    """(device, connection-name, ssid) of the active WiFi link, or Nones."""
    r = _sudo_nmcli(["-t", "-f", "NAME,DEVICE,TYPE,STATE", "connection", "show", "--active"])
    for line in (r.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2] == "802-11-wireless":
            name, dev = parts[0], parts[1]
            ssid = _sudo_nmcli(["-g", "802-11-wireless.ssid", "connection", "show", name]).stdout.strip()
            return dev, name, ssid
    return None, None, None


def probe(cfg: dict) -> bool:
    """True if real internet is reachable (TLS handshake to any provider)."""
    ctx = ssl.create_default_context()
    for host in cfg["providers"]:
        try:
            with socket.create_connection((host, 443), timeout=cfg["tls_timeout_s"]) as raw:
                with ctx.wrap_socket(raw, server_hostname=host):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def visible_ssids() -> list[str]:
    r = _sudo_nmcli(["-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "yes"])
    rows = []
    for line in (r.stdout or "").splitlines():
        ssid, _, sig = line.rpartition(":")
        if not ssid:
            ssid, sig = "", line
        try:
            rows.append((ssid, int(sig)))
        except ValueError:
            continue
    rows.sort(key=lambda x: -x[1])
    seen, out = set(), []
    for ssid, _ in rows:
        if ssid and ssid not in seen:
            seen.add(ssid)
            out.append(ssid)
    return out


def connection_for_ssid(ssid: str) -> str | None:
    r = _sudo_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    for line in (r.stdout or "").splitlines():
        name, _, typ = line.partition(":")
        if typ != "802-11-wireless":
            continue
        s = _sudo_nmcli(["-g", "802-11-wireless.ssid", "connection", "show", name]).stdout.strip()
        if s == ssid:
            return name
    return None


def try_up(ssid: str, cfg: dict) -> bool:
    name = connection_for_ssid(ssid)
    if not name:
        return False
    log(f"activating trusted SSID {ssid!r} (profile {name!r})")
    _sudo_nmcli(["connection", "up", name])
    time.sleep(cfg["settle_s"])
    return probe(cfg)


def _state_path(cfg: dict) -> Path:
    return Path(cfg.get("state_file") or DEFAULTS["state_file"]).expanduser()


def load_state(cfg: dict) -> dict:
    try:
        d = json.loads(_state_path(cfg).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(cfg: dict, status: str, sig: str):
    try:
        p = _state_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"status": status, "sig": sig,
                                 "at": datetime.now(timezone.utc).isoformat()}))
    except OSError:
        pass


def alert(cfg: dict, text: str, dry_run: bool):
    if dry_run:
        print(text)
        return
    cmd = (cfg.get("alert_cmd") or "").strip()
    if not cmd:
        print(text)
        return
    try:
        subprocess.run([*shlex.split(cmd), text], timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001
        log(f"alert_cmd failed ({exc}); falling back to stdout")
        print(text)


def run(cfg: dict, dry_run: bool) -> int:
    dev, conn, ssid = active_wifi()
    if not dev:
        # No WiFi link at all (ethernet-only host, or radio off): stay silent.
        return 0

    online = probe(cfg)
    prev = load_state(cfg)

    if online:
        save_state(cfg, "ok", "")
        if prev.get("status") not in (None, "ok"):
            alert(cfg, f"✅ wifi-uplink-guard: internet restored on {ssid!r} "
                       f"({datetime.now().strftime('%H:%M')})", dry_run)
        return 0

    klass = classify(ssid or "", cfg)
    visible = visible_ssids()
    trusted_visible = [s for s in visible if classify(s, cfg) == "trusted"]

    if klass != "trusted" and trusted_visible:
        for cand in trusted_visible:
            if cand == ssid:
                continue
            if try_up(cand, cfg):
                save_state(cfg, "ok", "")
                alert(cfg, f"✅ wifi-uplink-guard: switched {ssid!r} -> {cand!r} "
                           f"(internet restored) at {datetime.now().strftime('%H:%M')}",
                      dry_run)
                return 0
        status = "no-trusted-worked"
        msg = (f"⚠️ wifi-uplink-guard: offline on {ssid!r} ({klass}); tried trusted "
               f"{trusted_visible} but none gave internet")
    elif klass == "trusted":
        status = f"trusted-down:{ssid}"
        msg = (f"⚠️ wifi-uplink-guard: trusted network {ssid!r} has no internet; "
               f"not switching to an open hotspot. trusted_visible={trusted_visible}")
    else:
        status = f"open-offline:{ssid}"
        msg = (f"⚠️ wifi-uplink-guard: offline on open hotspot {ssid!r}; no trusted "
               f"SSID in range (visible={len(visible)})")

    if prev.get("status") == status:
        save_state(cfg, status, status)
        return 0  # unchanged since the last alert — stay silent

    save_state(cfg, status, status)
    alert(cfg, msg, dry_run)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one tick (default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect + report, never activate or alert")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0
    if args.json:
        dev, conn, ssid = active_wifi()
        print(json.dumps({"device": dev, "connection": conn, "ssid": ssid,
                          "online": probe(cfg),
                          "class": classify(ssid or "", cfg)}))
        return 0
    return run(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
