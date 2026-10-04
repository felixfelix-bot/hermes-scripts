#!/usr/bin/env python3
"""bandwidth_meter.py — meter the metered local gateway and attribute usage.

The home gateway cap is 350 GB/month (resets on the 1st). This samples, per
host, the bytes moved by:
  * each physical egress interface (``/proc/net/dev`` deltas),
  * selected systemd services (``IPAccounting=yes`` -> IPIngress/EgressBytes),
  * each Docker container (``docker stats`` cumulative NetIO deltas),
and appends deltas to ``~/.hermes/state/bandwidth.db``. ``--report`` rolls the
current cycle up by scope/name; ``--guard`` returns ok/warn/pause for the
budget guard. Process-level attribution (bpftrace/nethogs) is a follow-up.

Usage:
  bandwidth_meter.py --once            # sample + record (timer)
  bandwidth_meter.py --report [--json] # current-cycle rollup
  bandwidth_meter.py --guard [--json]  # budget guard decision
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = {
    "budget_gb": 350,
    "cycle_day": 1,
    "warn_pct": 70,
    "pause_pct": 80,
    "services": [
        "syncthing@c03rad0r.service",
        "syncthing@root.service",
        "hermes-gateway.service",
        "zai-proxy.service",
    ],
    "exclude_iface_prefixes": [
        "lo", "docker", "br-", "veth", "virbr", "loomtap", "vnet",
        "fips", "tun-", "wt0", "p2p-", "wwp", "tap",
    ],
}

SAMPLE_SQL = """
CREATE TABLE IF NOT EXISTS samples (
  ts REAL, host TEXT, scope TEXT, name TEXT, rx INTEGER, tx INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS counters (
  scope TEXT, name TEXT, ts REAL, rx INTEGER, tx INTEGER,
  PRIMARY KEY (scope, name)
);
"""


def default_paths() -> tuple[Path, Path]:
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return (home / "bot" / "bandwidth.json",
            home / "state" / "bandwidth.db")


# ---------------------------------------------------------------- pure logic
def parse_proc_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Parse ``/proc/net/dev`` -> ``{iface: (rx_bytes, tx_bytes)}``."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        fields = rest.split()
        if len(fields) < 9:
            continue
        out[iface.strip()] = (int(fields[0]), int(fields[8]))
    return out


def is_physical_iface(name: str, exclude_prefixes: list[str],
                      sysfs_root: str = "/sys/class/net") -> bool:
    """True for real NICs (excludes virtual/overlay/wwan/docker interfaces)."""
    if any(name.startswith(p) for p in exclude_prefixes):
        return False
    if name == "lo":
        return False
    # A physical NIC has a sysfs ``device`` symlink; virtual ones do not.
    device = Path(sysfs_root) / name / "device"
    if Path(sysfs_root).is_dir() and not device.exists():
        return False
    return True


def cycle_start_ts(now: float, cycle_day: int) -> float:
    """Epoch of the most recent ``cycle_day`` (1-based) at 00:00 local time."""
    dt = datetime.fromtimestamp(now)
    day = max(1, min(28, int(cycle_day)))
    year, month = dt.year, dt.month
    if dt.day < day:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return datetime(year, month, day).timestamp()


def guard_level(total_bytes: float, budget_gb: float,
                warn_pct: float, pause_pct: float) -> str:
    if budget_gb <= 0:
        return "ok"
    used_pct = (total_bytes / (budget_gb * 1024 ** 3)) * 100
    if used_pct >= pause_pct:
        return "pause"
    if used_pct >= warn_pct:
        return "warn"
    return "ok"


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def delta(prev: tuple[int, int] | None, cur: tuple[int, int]) -> tuple[int, int]:
    """Non-negative (rx,tx) delta; resets/wraps yield 0 rather than huge values."""
    if prev is None:
        return (0, 0)
    return (max(0, cur[0] - prev[0]), max(0, cur[1] - prev[1]))


def parse_docker_netio(text: str) -> dict[str, tuple[int, int]]:
    """Parse ``docker stats --no-stream --format '{{.Name}} {{.NetIO}}'``."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or "/" not in parts[1]:
            continue
        name = parts[0]
        rx_s, tx_s = (x.strip() for x in parts[1].split("/", 1))
        out[name] = (_parse_size(rx_s), _parse_size(tx_s))
    return out


_SIZE_RE = re.compile(r"([0-9.]+)\s*([kKmMgGtT]?)[bB]")


def _parse_size(text: str) -> int:
    m = _SIZE_RE.match(text.strip())
    if not m:
        return 0
    val = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    return int(val * mult.get(m.group(2).lower(), 1))


# ---------------------------------------------------------------- db + io
def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SAMPLE_SQL)
    return conn


def _load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(path.read_text()))
    except Exception:
        pass
    return cfg


def _prev(conn, scope: str, name: str) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT rx, tx FROM counters WHERE scope=? AND name=?",
        (scope, name)).fetchone()
    return (row[0], row[1]) if row else None


def _record(conn, ts: float, host: str, scope: str, name: str,
            cur: tuple[int, int]) -> tuple[int, int]:
    d = delta(_prev(conn, scope, name), cur)
    if d != (0, 0):
        conn.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)",
                     (ts, host, scope, name, d[0], d[1]))
    conn.execute(
        "INSERT OR REPLACE INTO counters VALUES (?,?,?,?,?)",
        (scope, name, ts, cur[0], cur[1]))
    return d


def _service_bytes(unit: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "IPIngressBytes", "-p", "IPEgressBytes"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    vals = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k] = v
    if "IPIngressBytes" not in vals:
        return None  # IPAccounting not enabled (or numeric overflow)
    try:
        return (int(vals["IPIngressBytes"]), int(vals["IPEgressBytes"]))
    except ValueError:
        return None


def sample_once(cfg: dict, db_path: Path, host: str) -> list[tuple]:
    ts = time.time()
    conn = _connect(db_path)
    written: list[tuple] = []
    try:
        with open("/proc/net/dev") as fh:
            ifaces = parse_proc_net_dev(fh.read())
        for name, cur in ifaces.items():
            if not is_physical_iface(name, cfg["exclude_iface_prefixes"]):
                continue
            d = _record(conn, ts, host, "iface", name, cur)
            if d != (0, 0):
                written.append(("iface", name, *d))

        for unit in cfg.get("services", []):
            cur = _service_bytes(unit)
            if cur is None:
                continue
            d = _record(conn, ts, host, "service", unit, cur)
            if d != (0, 0):
                written.append(("service", unit, *d))

        try:
            out = subprocess.run(
                ["docker", "stats", "--no-stream",
                 "--format", "{{.Name}} {{.NetIO}}"],
                capture_output=True, text=True, timeout=20).stdout
            for name, cur in parse_docker_netio(out).items():
                d = _record(conn, ts, host, "container", name, cur)
                if d != (0, 0):
                    written.append(("container", name, *d))
        except Exception:
            pass

        conn.commit()
    finally:
        conn.close()
    return written


def report(db_path: Path, cycle_day: int, now: float | None = None) -> dict:
    now = now or time.time()
    start = cycle_start_ts(now, cycle_day)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT scope, name, SUM(rx), SUM(tx) FROM samples WHERE ts>=? "
            "GROUP BY scope, name ORDER BY SUM(rx)+SUM(tx) DESC", (start,)).fetchall()
    finally:
        conn.close()
    total = sum(r[2] + r[3] for r in rows)
    return {"cycle_start": start, "total_bytes": total,
            "rows": [{"scope": r[0], "name": r[1], "rx": r[2], "tx": r[3]} for r in rows]}


def main(argv: list[str]) -> int:
    cfg_path, db_path = default_paths()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(cfg_path))
    ap.add_argument("--db", default=str(db_path))
    ap.add_argument("--host", default=os.uname().nodename)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--guard", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = _load_config(Path(args.config))
    db = Path(args.db)

    if args.once:
        written = sample_once(cfg, db, args.host)
        print(f"recorded {len(written)} deltas -> {db}")
        return 0

    rep = report(db, cfg.get("cycle_day", 1))
    level = guard_level(rep["total_bytes"], cfg.get("budget_gb", 350),
                        cfg.get("warn_pct", 70), cfg.get("pause_pct", 80))
    if args.guard:
        payload = {"level": level, "total_bytes": rep["total_bytes"],
                   "total": format_bytes(rep["total_bytes"]),
                   "budget_gb": cfg.get("budget_gb")}
        print(json.dumps(payload) if args.json else f"guard: {level} "
              f"({payload['total']} used)")
        return {"ok": 0, "warn": 0, "pause": 2}[level]

    if args.json:
        print(json.dumps({**rep, "level": level,
                          "cycle_start_iso": datetime.fromtimestamp(
                              rep["cycle_start"], timezone.utc).isoformat()}))
    else:
        print(f"cycle since {datetime.fromtimestamp(rep['cycle_start']).isoformat()} "
              f"— total {format_bytes(rep['total_bytes'])} (guard: {level})")
        for r in rep["rows"][:20]:
            print(f"  {r['scope']:10} {r['name']:34} "
                  f"rx {format_bytes(r['rx'])} tx {format_bytes(r['tx'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
