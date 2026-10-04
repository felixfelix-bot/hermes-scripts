#!/usr/bin/env python3
"""fleet_health.py — outcome-relative health + efficiency signal for the fleet.

Extends the raw heartbeat with an EFFICIENCY dimension: is the current spend
justified by the output being produced? Output = kanban completions + PR merges
(``fleet_outcomes.json`` gauge). Spend = cost from ``zai_usage.db.api_calls``.
``waste_ratio`` = current cost-per-unit / trailing median cost-per-unit.

Writes ``~/.hermes/bot/fleet_health.json`` (consumed by system-bleed-guard.py's
efficiency detector and fleet_arbiter.py) and, with ``--push``, publishes a
signed JSON event to the shared OrangeSync group for peer/operator visibility.

Pure stdlib. Never raises; each metric falls back safe.

Usage:
  fleet_health.py                 # compute + write
  fleet_health.py --json          # print
  fleet_health.py --push          # also publish signed event to buzz
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
OUT = BOT / "fleet_health.json"
HISTORY = BOT / "fleet_health_history.json"
OUTCOMES = BOT / "fleet_outcomes.json"
USAGE_DB = BOT / "zai_usage.db"
FLEET_CFG = BOT / "fleet.json"
OPS_CFG = BOT / "hermes_ops.json"
BOARDS_GLOB = str(HERMES / "kanban" / "boards" / "*" / "kanban.db")

WINDOW_S = 3600
HISTORY_KEEP = 168            # 7 days of hourly buckets
COST_PER_UNIT_FLOOR = 0.02    # $ floor so a zero-baseline can't divide by ~0
SPEND_WASTE_FLOOR = 0.10      # below this spend/hr we never call it waste
STATIC_WASTE_CEIL = 25.0      # cap the no-output ratio for sanity


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write_json_atomic(p: Path, payload: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(p)


def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            try:
                out[k.strip()] = int(rest.split()[0])
            except (IndexError, ValueError):
                continue
    except Exception:
        pass
    return out


def _system() -> dict:
    import shutil
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    nproc = os.cpu_count() or 1
    try:
        du = shutil.disk_usage(str(HERMES))
        disk_free_gb = round(du.free / 1e9, 1)
    except Exception:
        disk_free_gb = 0.0
    # authoritative live workers = reserved admission slots with a live PID
    workers = 0
    slots = BOT / ".fleet_slots"
    if slots.is_dir():
        for f in slots.glob("*.slot"):
            try:
                os.kill(int(f.stem), 0)
                workers += 1
            except (ProcessLookupError, ValueError):
                continue
            except PermissionError:
                workers += 1
    return {
        "load1": round(load1, 2),
        "load1_per_cpu": round(load1 / nproc, 2),
        "nproc": nproc,
        "mem_total_mb": total // 1024,
        "mem_available_mb": avail // 1024,
        "swap_used_mb": (swap_total - swap_free) // 1024,
        "disk_free_gb": disk_free_gb,
        "hermes_workers": workers,
    }


def _spend(window_s: int = WINDOW_S) -> dict:
    if not USAGE_DB.exists():
        return {"spend": 0.0, "tokens": 0, "calls": 0}
    try:
        c = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=5)
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(total_tokens),0), COUNT(*) "
            "FROM api_calls WHERE ts >= ?",
            (time.time() - window_s,),
        ).fetchone()
        c.close()
        return {"spend": float(row[0] or 0.0), "tokens": int(row[1] or 0),
                "calls": int(row[2] or 0)}
    except Exception:
        return {"spend": 0.0, "tokens": 0, "calls": 0}


def _completions(window_s: int = WINDOW_S) -> int:
    n = 0
    for p in glob.glob(BOARDS_GLOB):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
            r = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('done','completed') "
                "AND completed_at >= ?",
                (time.time() - window_s,),
            ).fetchone()
            n += int(r[0] or 0)
            c.close()
        except Exception:
            continue
    return n


def _merges() -> int:
    d = _read_json(OUTCOMES, {})
    try:
        return int(d.get("merges_last_hour", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _waste_ratio(spend_1h: float, units_1h: int) -> tuple[float, str, float]:
    """Return (waste_ratio, reason, cost_per_unit)."""
    hist = _read_json(HISTORY, {"buckets": []})
    buckets = hist.get("buckets", [])
    prior_cpu = [b["cost_per_unit"] for b in buckets
                 if b.get("units", 0) > 0 and b.get("cost_per_unit")]
    median_cpu = statistics.median(prior_cpu) if prior_cpu else None

    if units_1h > 0:
        cpu = spend_1h / units_1h
        base = max(median_cpu or cpu, COST_PER_UNIT_FLOOR)
        ratio = cpu / base if base else 1.0
        reason = f"${cpu:.3f}/unit vs median ${base:.3f}"
        return round(ratio, 2), reason, round(cpu, 4)

    # No output in the window.
    if spend_1h < SPEND_WASTE_FLOOR:
        return 0.0, f"idle (${spend_1h:.2f}/hr, 0 units)", 0.0
    base = max(median_cpu or COST_PER_UNIT_FLOOR, COST_PER_UNIT_FLOOR)
    ratio = min(spend_1h / base, STATIC_WASTE_CEIL)
    reason = f"${spend_1h:.2f}/hr with 0 units (median ${base:.3f}/unit)"
    return round(ratio, 2), reason, 0.0


def _fleet_cap() -> int:
    cap = int(os.environ.get("HERMES_FLEET_CAP", "4") or 4)
    try:
        d = json.loads((BOT / ".fleet_cap").read_text())
        c = int(d.get("cap", cap))
        if 0 <= c < cap:
            cap = c
    except Exception:
        pass
    return cap


def _fit() -> dict:
    """Local fit profile (repos/capabilities/exclusions/max_class)."""
    return _read_json(BOT / "fleet_fit.json", {})


def headroom_score(h: dict) -> float:
    """Normalized 0..1 spare-capacity score (min across dimensions).

    OPERATOR 2026-09-11 (K.1): the scalar used to decide which node should take
    a heavy task. 0 = saturated, 1 = fully idle.
    """
    try:
        nproc = float(h.get("nproc", 4) or 4)
        load = float(h.get("load1_per_cpu", 0) or 0)
        mem_total = float(h.get("mem_total_mb", 1) or 1)
        mem_avail = float(h.get("mem_available_mb", 0) or 0)
        workers = float(h.get("hermes_workers", 0) or 0)
        cap = float(h.get("fleet_cap", 4) or 4)
        spare_cpu = max(0.0, min(1.0, 1.0 - load))
        mem_frac = max(0.0, min(1.0, mem_avail / mem_total))
        mem_spare = max(0.0, min(1.0, (mem_frac - 0.10) / 0.50))
        worker_spare = max(0.0, min(1.0, 1.0 - (workers / cap if cap else 1.0)))
        # Phase V6.5 — disk spare so placement/balancing favour the roomy node
        # (<=10G free -> 0, >=100G free -> 1).
        disk_free = float(h.get("disk_free_gb", 0) or 0)
        disk_spare = max(0.0, min(1.0, (disk_free - 10.0) / 90.0))
        return round(min(spare_cpu, mem_spare, worker_spare, disk_spare), 3)
    except Exception:
        return 0.0


def compute() -> dict:
    cfg = _read_json(FLEET_CFG, {})
    sysd = _system()
    spend = _spend()
    comp = _completions()
    merges = _merges()
    units = comp + merges
    ratio, reason, cpu = _waste_ratio(spend["spend"], units)

    health = {
        "node": cfg.get("node") or socket.gethostname(),
        "role": cfg.get("role", "worker"),
        "ts": int(time.time()),
        "iso": datetime.now(timezone.utc).isoformat(),
        **sysd,
        "fleet_cap": _fleet_cap(),
        "spend_1h": round(spend["spend"], 4),
        "tokens_1h": spend["tokens"],
        "calls_1h": spend["calls"],
        "completions_1h": comp,
        "merges_1h": merges,
        "units_1h": units,
        "cost_per_unit": cpu,
        "waste_ratio": ratio,
        "waste_reason": reason,
    }
    fit = _fit()
    if fit:
        health["fit"] = {
            "repos": fit.get("repos", []),
            "languages": fit.get("languages", []),
            "capabilities": fit.get("capabilities", []),
            "exclusions": fit.get("exclusions", []),
            "prefer_classes": fit.get("prefer_classes", []),
            "max_class": fit.get("max_class", "heavy"),
        }
    health["headroom_score"] = headroom_score(health)

    # Append hourly bucket (one per clock hour, latest wins) for the baseline.
    hist = _read_json(HISTORY, {"buckets": []})
    bucket = hist.setdefault("buckets", [])
    hour = int(time.time() // 3600)
    bucket = [b for b in bucket if b.get("hour") != hour][-HISTORY_KEEP:]
    bucket.append({"hour": hour, "spend": round(spend["spend"], 4),
                   "units": units, "cost_per_unit": cpu})
    _write_json_atomic(HISTORY, {"buckets": bucket})
    return health


def _nak_path() -> str:
    for cand in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
                 "/usr/bin/nak"):
        if Path(cand).exists():
            return cand
    return "nak"


def _push_peers(health: dict) -> dict:
    """Copy fleet_health.json to each peer's peers/<node>.health.json over SSH.

    Keeps the raw heartbeat file (peers/<node>.json) untouched; the arbiter
    prefers the richer *.health.json when both exist.
    """
    cfg = _read_json(FLEET_CFG, {})
    node = health["node"]
    body = json.dumps(health, indent=1)
    results = {}
    for peer in cfg.get("peers", []) or []:
        name = peer.get("name") or "peer"
        hosts = peer.get("hosts") or ([peer.get("host")] if peer.get("host") else [])
        user = peer.get("user", "c03rad0r")
        key = os.path.expanduser(peer.get("key", "~/.ssh/id_dq05"))
        ok = False
        for host in hosts:
            if not host:
                continue
            remote = f"{user}@{host}"
            try:
                r = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                     "-o", "ConnectTimeout=8", "-i", key, remote,
                     f"cat > ~/.hermes/bot/peers/{node}.health.json"],
                    input=body, text=True, timeout=20)
                if r.returncode == 0:
                    ok = True
                    break
            except Exception:
                continue
        results[name] = ok
    return results


def _publish(health: dict) -> bool:
    ops = _read_json(OPS_CFG, {})
    nsec = os.path.expanduser(ops.get("node_nsec", "~/.hermes/keys/hermes-ops/cobrador.nsec"))
    relay = ops.get("orange_relay", "wss://relay.orangesync.tech")
    group = ops.get("orange_group")
    nsec_path = Path(nsec)
    if not group or not nsec_path.exists():
        return False
    nak = _nak_path()
    try:
        key = nsec_path.read_text().strip()
    except OSError:
        return False
    content = json.dumps({
        "type": "fleet-health",
        "node": health["node"],
        "role": health["role"],
        "ts": health["ts"],
        "load1_per_cpu": health["load1_per_cpu"],
        "mem_available_mb": health["mem_available_mb"],
        "workers": health["hermes_workers"],
        "fleet_cap": health.get("fleet_cap"),
        "headroom_score": health.get("headroom_score"),
        "spend_1h": health["spend_1h"],
        "units_1h": health["units_1h"],
        "waste_ratio": health["waste_ratio"],
        "waste_reason": health["waste_reason"],
    }, separators=(",", ":"))
    cmd = [nak, "event", "-k", "9", "-c", content,
           "-t", f"h={group}", "-t", "client=hermes-fleet", "-t", "t=fleet-health",
           "--sec", key, "--auth", relay]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        return r.returncode == 0 and "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def _preserve_components(health: dict, prev: dict) -> dict:
    """Carry the component block forward across a resource-health rewrite.

    fleet_component_health.py --merge owns `components`/`components_ts` in
    fleet_health.json; this writer owns the resource fields. A blind overwrite
    used to wipe components, so the guard/dashboard saw none and the
    peers/<node>.health.json copies (and the dead-man state.db watchdog) carried
    none either.
    """
    for k in ("components", "components_ts"):
        if k not in health and k in (prev or {}):
            health[k] = prev[k]
    return health


def main(argv: list[str]) -> int:
    health = compute()
    _preserve_components(health, _read_json(OUT, {}) or {})
    _write_json_atomic(OUT, health)
    if "--json" in argv:
        print(json.dumps(health, indent=1))
    if "--push" in argv:
        # --no-buzz keeps the SSH peer copy (which every cross-node view needs)
        # while suppressing the Buzz publish (telemetry-flood control).
        buzz = False if "--no-buzz" in argv else _publish(health)
        peers = _push_peers(health)
        print(f"[fleet-health] {health['node']}: waste_ratio={health['waste_ratio']} "
              f"units_1h={health['units_1h']} spend_1h=${health['spend_1h']} "
              f"buzz={buzz} peers={peers}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
