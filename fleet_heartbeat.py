#!/usr/bin/env python3
"""fleet_heartbeat.py — publish this node's capacity for the two-node Hermes fleet.

Reads a small config (~/.hermes/bot/fleet.json), collects host metrics, writes
~/.hermes/bot/fleet_heartbeat.json, and (with --push) copies it to each peer's
~/.hermes/bot/peers/<node>.json over the existing SSH trust.

Pure stdlib. Never raises on a missing metric; each field falls back to a safe
default. See docs/PLAN-fleet-dispatch.md (D-121).

Usage:
  fleet_heartbeat.py                 # collect + write local heartbeat
  fleet_heartbeat.py --push          # also copy to every configured peer
  fleet_heartbeat.py --push --strict  # exit 1 if any peer push fails
  fleet_heartbeat.py --json          # print the heartbeat to stdout
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
CONFIG = BOT / "fleet.json"
OUT = BOT / "fleet_heartbeat.json"
PEER_DIR = BOT / "peers"


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default if default is not None else {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


def _meminfo() -> dict:
    data: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            try:
                data[key.strip()] = int(rest.split()[0])  # kB
            except (IndexError, ValueError):
                continue
    except Exception:
        pass
    return data


def load_config() -> tuple[str, str, list[dict], dict]:
    cfg = _read_json(CONFIG, {})
    node = cfg.get("node") or socket.gethostname()
    role = cfg.get("role", "worker")
    peers = cfg.get("peers", []) or []
    caps = cfg.get("caps", {}) or {}
    return node, role, peers, caps


def hermes_version() -> str:
    candidates = [
        shutil.which("hermes"),
        str(HOME / ".hermes/hermes-agent/venv/bin/hermes"),
        str(HOME / ".local/bin/hermes"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            out = _run([cand, "--version"])
            if out:
                return out.splitlines()[0]
    return ""


def node_pubkey() -> str:
    """This node's ops x-only pubkey hex (Phase U node id + responder election).

    Prefer the sibling `.npub` written by role 19-ops-identity (no external
    dependency), then derive with `cryptography`, then fall back to `nak`.
    """
    ops = _read_json(BOT / "hermes_ops.json", {}) or {}
    p = ops.get("node_nsec")
    if not p:
        return ""
    nsec = Path(os.path.expanduser(p))
    if not nsec.exists():
        return ""
    npub = nsec.with_suffix(".npub")
    if npub.exists():
        val = npub.read_text().strip()
        if len(val) == 64:
            return val
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        k = ec.derive_private_key(int(nsec.read_text().strip(), 16), ec.SECP256K1())
        return k.public_key().public_numbers().x.to_bytes(32, "big").hex()
    except Exception:  # noqa: BLE001
        pass
    nak = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                            "/usr/local/bin/nak", "/usr/bin/nak"]
                if Path(c).exists()), "nak")
    return _run([nak, "key", "public", nsec.read_text().strip()], timeout=15)


def hermes_node_id() -> str:
    """FIPS-style node id: first 16 bytes of SHA-256(x-only ops pubkey) (Phase U)."""
    pk = node_pubkey().strip()
    try:
        return hashlib.sha256(bytes.fromhex(pk)).hexdigest()[:32]
    except ValueError:
        return ""


def fips_addr() -> str:
    """This node's FIPS mesh IPv6 (fips0 global), for root-forwarding (Phase U)."""
    out = _run(["ip", "-6", "-o", "addr", "show", "dev", "fips0", "scope", "global"], timeout=5)
    for tok in out.split():
        if tok.startswith("fd") and "/" in tok:
            return tok.split("/")[0]
    return ""


def _root_status() -> dict:
    """Hermes-root election status (Phase U); fail-soft to a non-root default."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fleet_root  # type: ignore
        return fleet_root.status()
    except Exception as exc:  # noqa: BLE001
        return {"is_root": False, "fresh_count": 1, "error": str(exc)}


def _static_cap_for(caps: dict, root_status: dict) -> int:
    """Root-aware dispatched-worker cap (Phase U4); fail-soft to the raw cap."""
    base = int(caps.get("static_cap", 3))
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fleet_root  # type: ignore
        return fleet_root.effective_cap(
            bool(root_status.get("is_root")),
            int(root_status.get("fresh_count", 1)),
            base,
            int(caps.get("root_offload_max", 0)),
        )
    except Exception:  # noqa: BLE001
        return base


def _pressure_block(caps: dict, static_cap: int | None = None) -> dict:
    """Compute the Kalman-smoothed adaptive worker cap for this node."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pressure  # type: ignore
        return pressure.evaluate(static_cap=int(caps.get("static_cap", 3) if static_cap is None else static_cap))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "can_dispatch": True, "smoothed_cap": None}


def collect(node: str, role: str, caps: dict | None = None) -> dict:
    caps = caps or {}
    root_st = _root_status()
    static_cap = _static_cap_for(caps, root_st)
    pressure = _pressure_block(caps, static_cap)
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_total_kb = mem.get("SwapTotal", 0)
    swap_free_kb = mem.get("SwapFree", 0)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    try:
        du = shutil.disk_usage(str(HOME))
        disk_free_gb = round(du.free / 1e9, 1)
        disk_total_gb = round(du.total / 1e9, 1)
        disk_free_pct = round(100.0 * du.free / du.total, 1) if du.total else 0.0
    except Exception:
        disk_free_gb = disk_total_gb = disk_free_pct = 0.0
    zai = _read_json(BOT / "zai_state.json", {})
    try:
        workers = int(_run(["pgrep", "-fc", "work kanban task"]) or 0)
    except ValueError:
        workers = 0
    nproc = os.cpu_count() or 1
    return {
        "node": node,
        "role": role,
        "hostname": socket.gethostname(),
        "pubkey": node_pubkey(),
        "hermes_node_id": hermes_node_id(),
        "fips_addr": fips_addr(),
        "ts": int(time.time()),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "nproc": nproc,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "load1_per_cpu": round(load1 / nproc, 2),
        "mem_total_mb": total_kb // 1024,
        "mem_available_mb": avail_kb // 1024,
        "mem_available_pct": round(100.0 * avail_kb / total_kb, 1) if total_kb else 0.0,
        "swap_total_mb": swap_total_kb // 1024,
        "swap_used_mb": (swap_total_kb - swap_free_kb) // 1024,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "disk_free_pct": disk_free_pct,
        "hermes_workers": workers,
        "router": {
            "throttle": bool(zai.get("throttle")),
            "quota_pause": bool(zai.get("quota_pause")),
            "session_pct": zai.get("session_pct", 0),
            "token_pct": zai.get("token_pct", 0),
            "ok": bool(zai.get("ok", True)),
        },
        "hermes_version": hermes_version(),
        "is_root": bool(root_st.get("is_root")),
        "fresh_count": root_st.get("fresh_count"),
        "static_cap_effective": static_cap,
        "active_cap": pressure.get("smoothed_cap"),
        "pressure": pressure,
    }


def _ssh_base(key: str | None) -> list[str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
    ]
    if key:
        cmd += ["-i", os.path.expanduser(key)]
    return cmd


def _classify(host: str) -> str:
    """Label a peer address by link type.

    Preference order (Phase V3.2): LAN > FIPS > NetBird > other — keep fleet
    traffic off the metered WAN wherever a local path exists.
    """
    if host.startswith("192.168.") or host.endswith(".local"):
        return "lan"
    if host.startswith("fd") and ":" in host:
        return "fips"
    if host.startswith("100.90."):
        return "netbird"
    return "other"


_LINK_RANK = {"lan": 0, "fips": 1, "netbird": 2, "other": 3}


def _order_hosts(candidates: list[str], last: str | None = None) -> list[str]:
    """Order peer addresses LAN > FIPS > NetBird > other (stable within class);
    the last-working address goes first inside its own class only."""
    ordered = sorted(candidates, key=lambda h: _LINK_RANK.get(_classify(h), 9))
    if last in ordered:
        rank = _LINK_RANK.get(_classify(last), 9)
        same = [h for h in ordered if _LINK_RANK.get(_classify(h), 9) == rank and h != last]
        rest = [h for h in ordered if _LINK_RANK.get(_classify(h), 9) != rank]
        ordered = [last] + same + rest
    return ordered


def _peer_hosts(peer: dict, routes: dict) -> list[str]:
    """Ordered candidate addresses: LAN > FIPS > NetBird, last-working first."""
    candidates = peer.get("hosts")
    if not isinstance(candidates, list) or not candidates:
        host = peer.get("host")
        candidates = [host] if host else []
    candidates = [h for h in candidates if h]
    last = (routes.get(peer.get("name")) or {}).get("host")
    return _order_hosts(candidates, last)


def push(peers: list[dict], payload: dict, node: str,
         extras: list[tuple[str, str]] | None = None) -> dict:
    """Copy the heartbeat (+ optional extra files) to each peer, trying LAN
    before NetBird. `extras` are (remote_filename, content) pairs. Returns
    per-peer bool."""
    results: dict[str, bool] = {}
    routes: dict[str, dict] = {}
    prev = _read_json(BOT / "fleet_routes.json", {}) or {}
    prev_routes = prev.get("routes", {}) or {}
    files = [(f"{node}.json", json.dumps(payload, indent=1))] + list(extras or [])
    for peer in peers:
        name = peer.get("name") or (peer.get("host") or "peer")
        user = peer.get("user", "c03rad0r")
        dest_dir = peer.get("remote_dir", "~/.hermes/bot/peers")
        ssh = _ssh_base(peer.get("key"))
        ok = False
        for host in _peer_hosts(peer, prev_routes):
            remote = f"{user}@{host}"
            try:
                subprocess.run(ssh + [remote, f"mkdir -p {dest_dir}"], timeout=12)
                good = True
                for fname, content in files:
                    proc = subprocess.run(
                        ssh + [remote, f"cat > {dest_dir}/{fname}"],
                        input=content, text=True, timeout=20,
                    )
                    if proc.returncode != 0:
                        good = False
                        break
                if good:
                    results[name] = True
                    routes[name] = {"host": host, "link": _classify(host)}
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            results[name] = False
    if routes:
        try:
            _write_json_atomic(BOT / "fleet_routes.json", {
                "ts": int(time.time()), "node": node, "routes": routes,
            })
        except Exception:
            pass
    return results


def main(argv: list[str]) -> int:
    node, role, peers, caps = load_config()
    payload = collect(node, role, caps)
    _write_json_atomic(OUT, payload)
    if "--json" in argv:
        print(json.dumps(payload, indent=1))
    if "--push" in argv:
        PEER_DIR.mkdir(parents=True, exist_ok=True)
        extras: list[tuple[str, str]] = []
        own = BOT / "ownership.json"
        if own.exists():
            try:
                extras.append((f"{node}.ownership.json", own.read_text()))
            except OSError:
                pass
        # shared usage telemetry (D-128 §2.7.1): sample + ship for symmetric Kalman
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import fleet_usage as fu  # type: ignore
            fu.write_sample(fu.sample(node))
            usage_dir = Path.home() / ".hermes" / "state" / "fleet-usage"
            for fname in (f"usage-{node}.json", f"usage_series-{node}.jsonl"):
                fp = usage_dir / fname
                if fp.exists():
                    extras.append((fname, fp.read_text()))
        except Exception:
            pass
        # private coordination ledger (D-128 8.8): ship over SSH, never the relay
        priv = Path.home() / ".hermes" / "state" / "fleet-private" / f"events-{node}.jsonl"
        if priv.exists():
            try:
                extras.append((f"events-{node}.jsonl", priv.read_text()))
            except OSError:
                pass
        results = push(peers, payload, node, extras)
        routes = _read_json(BOT / "fleet_routes.json", {}).get("routes", {})
        links = {name: (routes.get(name) or {}).get("link", "unreachable") for name in results}
        print(f"[fleet-heartbeat] {node}: pushed {results} via {links}", file=sys.stderr)
        if "--strict" in argv and results and not all(results.values()):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
