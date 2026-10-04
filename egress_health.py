#!/usr/bin/env python3
"""egress_health.py — pick the fastest healthy egress path and prefer it.

D-136. The fleet's LLM providers are reached through the local flat router;
when the active uplink degrades (slow TLS handshakes, dropped packets) every
provider candidate times out and the gateway reports "all providers exhausted".
The uplink that is *healthy* changes over time (ethernet VPN up/down, WiFi
roaming), so pick it dynamically.

For every candidate interface this:
  1. checks carrier + a global IPv4 address,
  2. pings its gateway and 1.1.1.1 bound to that interface,
  3. times a TLS handshake to each configured provider host bound to that
     interface,
then ranks the interfaces and, when ``enforce`` is on, prefers the winner by
lowering its NetworkManager route metric (best=100, others=200+n) and
re-applying the device.  If nothing passes, routes are left untouched and an
alert is emitted — never pin the fleet to a dead path.

Config: ~/.hermes/bot/egress_health.json (defaults created on first run)
  {"interfaces": [],            # [] = auto-discover physical uplinks
   "exclude_prefixes": ["docker","br-","veth","virbr","loomtap","vnet",
                        "fips","tun-","wt0","lo","p2p-"],
   "providers": ["api.deepinfra.com","api.deepseek.com","openrouter.ai"],
   "tls_timeout_s": 8, "ping_wait_s": 2, "min_ok_providers": 1,
   "enforce": true, "alert_cmd": ""}

State: ~/.hermes/bot/egress_health_state.json
Usage: egress_health.py [--once] [--dry-run] [--json] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
BOT = HERMES / "bot"
CONFIG = BOT / "egress_health.json"
STATE = BOT / "egress_health_state.json"

DEFAULTS = {
    "interfaces": [],
    "exclude_prefixes": ["docker", "br-", "veth", "virbr", "loomtap", "vnet",
                         "fips", "tun-", "wt0", "lo", "p2p-"],
    "providers": ["api.deepinfra.com", "api.deepseek.com", "openrouter.ai"],
    "tls_timeout_s": 8,
    "ping_wait_s": 2,
    "min_ok_providers": 1,
    "enforce": True,
    "alert_cmd": "",
    "preferred_metric": 100,
}


def log(*p) -> None:
    print(f"[egress-health] {datetime.now(timezone.utc).isoformat()}", *p,
          flush=True)


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except Exception:
        BOT.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG.write_text(json.dumps(cfg, indent=1))
        except OSError:
            pass
    return cfg


def _excluded(name: str, prefixes: list[str]) -> bool:
    return any(name == p or name.startswith(p) for p in prefixes)


def discover_interfaces(excludes: list[str]) -> list[str]:
    """Physical uplinks: UP, global IPv4 address, not excluded."""
    r = _run(["ip", "-j", "-4", "addr", "show"])
    out: list[str] = []
    try:
        for dev in json.loads(r.stdout or "[]"):
            name = dev.get("ifname", "")
            if dev.get("operstate") != "UP" or _excluded(name, excludes):
                continue
            for ai in dev.get("addr_info", []):
                if ai.get("family") == "inet" and ai.get("scope") == "global":
                    out.append(name)
                    break
    except json.JSONDecodeError:
        pass
    return out


def gateways() -> dict[str, str]:
    r = _run(["ip", "-j", "route", "show", "default"])
    gw: dict[str, str] = {}
    try:
        for route in json.loads(r.stdout or "[]"):
            dev, via = route.get("dev"), route.get("gateway")
            if dev and via and dev not in gw:
                gw[dev] = via
    except json.JSONDecodeError:
        pass
    return gw


def _ping(iface: str, host: str, wait: int) -> bool:
    return _run(["ping", "-c", "2", "-W", str(wait), "-I", iface, host],
                timeout=wait * 3 + 4).returncode == 0


def _tls_ms(iface: str, host: str, timeout: int) -> float | None:
    """Return TLS connect time in ms, or None on failure."""
    r = _run(["curl", "--interface", iface, "-sS", "-m", str(timeout),
              "-o", "/dev/null", "-w", "%{time_appconnect}",
              f"https://{host}/"], timeout=timeout + 4)
    if r.returncode != 0:
        return None
    try:
        val = float((r.stdout or "").strip())
    except ValueError:
        return None
    return val * 1000 if val > 0 else None


def probe_interface(iface: str, gw: str | None, providers: list[str],
                    tls_timeout: int, ping_wait: int) -> dict:
    gw_ok = _ping(iface, gw, ping_wait) if gw else False
    net_ok = _ping(iface, "1.1.1.1", ping_wait)
    times: list[float] = []
    ok_providers = 0
    for host in providers:
        ms = _tls_ms(iface, host, tls_timeout)
        if ms is not None:
            ok_providers += 1
            times.append(ms)
    times.sort()
    median = times[len(times) // 2] if times else float("inf")
    return {"iface": iface, "gw_ok": gw_ok, "net_ok": net_ok,
            "ok_providers": ok_providers, "median_tls_ms": median}


def score(res: dict) -> float:
    """Higher is better. Provider success dominates; latency breaks ties."""
    if res["ok_providers"] <= 0:
        return -1.0
    base = res["ok_providers"] * 1000.0
    base += 100.0 if res["net_ok"] else 0.0
    ms = res["median_tls_ms"]
    if ms != float("inf"):
        base -= ms
    return base


def select_best(results: list[dict], min_ok: int) -> dict | None:
    viable = [r for r in results if r["ok_providers"] >= min_ok]
    if not viable:
        return None
    return max(viable, key=score)


def _nm_connection(iface: str) -> str | None:
    if not shutil.which("nmcli"):
        return None
    r = _run(["nmcli", "-t", "-f", "DEVICE,CONNECTION", "device"], timeout=10)
    for line in (r.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == iface and parts[1]:
            return parts[1]
    return None


def apply_preference(winner: str, others: list[str], base_metric: int,
                     verbose: bool) -> dict:
    """Lower the winner's NM route metric; raise the others'. Route ops need sudo."""
    changes: dict[str, int] = {}
    plan = [(winner, base_metric)] + [(o, base_metric + 100 + i)
                                      for i, o in enumerate(others)
                                      if o != winner]
    for iface, metric in plan:
        conn = _nm_connection(iface)
        if not conn:
            continue
        r = _run(["sudo", "-n", "nmcli", "connection", "modify", conn,
                  "ipv4.route-metric", str(metric)], timeout=15)
        if r.returncode != 0 and verbose:
            log(f"metric set failed {iface}: {r.stderr.strip()[:120]}")
            continue
        _run(["sudo", "-n", "nmcli", "device", "reapply", iface], timeout=20)
        changes[iface] = metric
    return changes


def emit_alert(cfg: dict, message: str) -> None:
    log("ALERT:", message)
    cmd = (cfg.get("alert_cmd") or os.environ.get("FLEET_ALERT_CMD") or "").strip()
    if cmd:
        try:
            subprocess.run(f'{cmd} {json.dumps(message)}', shell=True,
                           timeout=30, capture_output=True)
        except Exception as exc:  # noqa: BLE001
            log("alert cmd failed:", exc)


def save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, indent=1))
    except OSError:
        pass


def run(once: bool, dry_run: bool, verbose: bool) -> int:
    cfg = load_config()
    ifaces = cfg.get("interfaces") or discover_interfaces(
        cfg["exclude_prefixes"])
    gw = gateways()
    if verbose:
        log("candidates:", ifaces, "gateways:", gw)
    results = [probe_interface(i, gw.get(i), cfg["providers"],
                               cfg["tls_timeout_s"], cfg["ping_wait_s"])
               for i in ifaces]
    best = select_best(results, cfg["min_ok_providers"])
    state = {"ts": time.time(), "interfaces": results,
             "chosen": best["iface"] if best else None,
             "chosen_score": round(score(best), 1) if best else None,
             "changes": {}, "enforced": False}

    if best is None:
        state["alert"] = "all egress paths failed"
        emit_alert(cfg, f"egress degraded: no path passed "
                        f"({len(results)} candidates)")
    elif cfg.get("enforce") and not dry_run:
        others = [r["iface"] for r in results]
        state["changes"] = apply_preference(best["iface"], others,
                                            cfg["preferred_metric"], verbose)
        state["enforced"] = bool(state["changes"])
    save_state(state)
    if verbose or best is None:
        for r in results:
            log(f"  {r['iface']}: ok={r['ok_providers']} "
                f"median_tls={r['median_tls_ms']:.0f}ms net={r['net_ok']}")
    log(f"chosen={state['chosen']} enforced={state['enforced']} "
        f"changes={state['changes']}")
    return 0 if best else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    if args.json:
        print(json.dumps(load_config(), indent=1))
        return 0
    return run(args.once, args.dry_run, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
