#!/usr/bin/env python3
"""pressure.py — Kalman-smoothed resource pressure and adaptive worker cap.

Portable, stdlib-only port of CobradorWave's gateway headroom governor
(`gateway/kanban_watchers.py:_compute_dispatch_headroom`) plus a 2-state pool
Kalman that smooths the resulting worker cap with hysteresis (replacing the
deleted `pool_kalman.py`).

Never raises; every metric falls back to a safe default. See
docs/PLAN-autonomous-dispatch.md (D-123).

CLI:
  pressure.py --json          # print decision, write ~/.hermes/bot/pressure.json
  pressure.py                 # human-readable summary
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
STATE = BOT / "pool_kalman.json"
OUT = BOT / "pressure.json"

# Raw breach thresholds (mirrors the CobradorWave governor).
RAW_THRESHOLDS = {
    "cpu_load": 8.0,
    "memory_pct": 85.0,
    "swap_used_pct": 80.0,
    "disk_used_pct": 90.0,
}
DEFAULT_STATIC_CAP = 3


def _run(cmd: list[str], timeout: int = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def collect_metrics() -> dict:
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            try:
                mem[k.strip()] = int(rest.split()[0])
            except (IndexError, ValueError):
                pass
    except Exception:
        pass
    total = mem.get("MemTotal", 1)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    try:
        disk = shutil.disk_usage(str(HOME))
        disk_used_pct = 100.0 * (disk.total - disk.free) / disk.total
    except Exception:
        disk_used_pct = 0.0
    try:
        workers = int(_run(["pgrep", "-fc", "work kanban task"]) or 0)
    except ValueError:
        workers = 0
    def _pct(used, tot):
        return round(100.0 * used / tot, 1) if tot else 0.0
    return {
        "ts": int(time.time()),
        "cpu_load": round(os.getloadavg()[0], 2),
        "memory_pct": _pct(total - avail, total),
        "swap_used_pct": _pct(swap_total - swap_free, swap_total),
        "disk_used_pct": round(disk_used_pct, 1),
        "worker_count": workers,
        "tokens": 0.0,
    }


class ResourceKalman:
    """Diagonal persistence Kalman (F=I) over the six resource dims.

    Matches the semantics of `multi_resource_kalman.py`: the mean is a smoothed
    estimate; the 95% band grows with covariance. We use it only for early
    warning (a dimension whose upper band crosses its threshold gets 0.5).
    """

    DIMS = ["tokens", "cpu_load", "memory_pct", "swap_used_pct", "disk_used_pct", "worker_count"]
    R = {"tokens": 1e6, "cpu_load": 0.5, "memory_pct": 5.0, "swap_used_pct": 10.0,
         "disk_used_pct": 2.0, "worker_count": 2.0}
    LIMITS = {"tokens": 5e7, "cpu_load": 8.0, "memory_pct": 85.0,
              "swap_used_pct": 80.0, "disk_used_pct": 85.0, "worker_count": 40}

    def __init__(self) -> None:
        self.x = {d: None for d in self.DIMS}
        self.p = {d: self.R[d] for d in self.DIMS}
        self.q = 1.0

    def update(self, obs: dict) -> None:
        for d in self.DIMS:
            z = obs.get(d)
            if z is None:
                continue
            if self.x[d] is None:
                self.x[d] = float(z)
                continue
            # predict (F=I) then update
            p = self.p[d] + self.q
            k = p / (p + self.R[d])
            self.x[d] = self.x[d] + k * (float(z) - self.x[d])
            self.p[d] = (1 - k) * p

    def warnings(self) -> list[str]:
        warn = []
        for d in self.DIMS:
            if self.x[d] is None:
                continue
            upper = self.x[d] + 1.96 * (self.p[d] ** 0.5)
            if upper >= self.LIMITS[d]:
                warn.append(d)
        return warn


def compute_headroom(metrics: dict, static_cap: int, kalman_warn: list[str],
                     llm_ok: bool = True) -> dict:
    per_dim: dict[str, float] = {}
    for res, thr in RAW_THRESHOLDS.items():
        v = metrics.get(res)
        if v is None:
            continue
        if v >= thr:
            per_dim[res] = 0.0
        elif v >= thr * 0.9:
            per_dim[res] = 0.5
        elif res in kalman_warn:
            per_dim[res] = 0.5
        else:
            per_dim[res] = 1.0
    per_dim["llm"] = 1.0 if llm_ok else 0.0
    min_headroom = min(per_dim.values()) if per_dim else 1.0
    if min_headroom <= 0.0:
        target = 0
    else:
        target = max(1, int(round(static_cap * min_headroom)))
    breaches = [k for k, v in per_dim.items() if v == 0.0]
    reason = "ok" if not breaches else "breach:" + ",".join(breaches)
    return {
        "per_dimension": per_dim,
        "min_headroom": min_headroom,
        "target_workers_raw": target,
        "can_dispatch": target > 0,
        "reason": reason,
    }


def _mat2_mul(a, b):
    return [[a[0][0] * b[0][0] + a[0][1] * b[1][0], a[0][0] * b[0][1] + a[0][1] * b[1][1]],
            [a[1][0] * b[0][0] + a[1][1] * b[1][0], a[1][0] * b[0][1] + a[1][1] * b[1][1]]]


class PoolKalman:
    """2-state [target, velocity] Kalman smoothing the worker cap with hysteresis."""

    Q = [[0.1, 0.0], [0.0, 0.02]]
    R = 1.0
    P0 = [[4.0, 0.0], [0.0, 1.0]]

    def __init__(self) -> None:
        self.x = [2.0, 0.0]
        self.p = [row[:] for row in self.P0]
        try:
            data = json.loads(STATE.read_text())
            self.x = [float(data["x"][0]), float(data["x"][1])]
            self.p = [[float(data["P"][0][0]), float(data["P"][0][1])],
                      [float(data["P"][1][0]), float(data["P"][1][1])]]
        except Exception:
            pass

    def update(self, measurement: float, dt: float = 60.0) -> float:
        f = [[1.0, dt], [0.0, 1.0]]
        # predict
        px0 = f[0][0] * self.x[0] + f[0][1] * self.x[1]
        px1 = f[1][0] * self.x[0] + f[1][1] * self.x[1]
        self.x = [px0, px1]
        fp = _mat2_mul(f, self.p)
        ft = [[f[0][0], f[1][0]], [f[0][1], f[1][1]]]
        fpft = _mat2_mul(fp, ft)
        self.p = [[fpft[0][0] + self.Q[0][0], fpft[0][1] + self.Q[0][1]],
                  [fpft[1][0] + self.Q[1][0], fpft[1][1] + self.Q[1][1]]]
        # update (H=[1,0])
        s = self.p[0][0] + self.R
        k0 = self.p[0][0] / s
        k1 = self.p[1][0] / s
        y = measurement - self.x[0]
        self.x = [self.x[0] + k0 * y, self.x[1] + k1 * y]
        newp = [[(1 - k0) * self.p[0][0], (1 - k0) * self.p[0][1]],
                [self.p[1][0] - k1 * self.p[0][0], self.p[1][1] - k1 * self.p[0][1]]]
        self.p = newp
        return self.x[0]

    def save(self) -> None:
        try:
            STATE.write_text(json.dumps({"x": self.x, "P": self.p, "ts": int(time.time())}))
        except Exception:
            pass


def evaluate(static_cap: int = DEFAULT_STATIC_CAP, llm_ok: bool = True) -> dict:
    metrics = collect_metrics()
    kal = ResourceKalman()
    # Seed from a short synthetic history so the band is meaningful on first run.
    for _ in range(3):
        kal.update(metrics)
    warn = kal.warnings()
    decision = compute_headroom(metrics, static_cap, warn, llm_ok)
    pool = PoolKalman()
    smoothed = pool.update(decision["target_workers_raw"], dt=60.0)
    pool.save()
    smoothed_int = int(round(smoothed))
    smoothed_int = max(0, min(smoothed_int, static_cap))
    if decision["target_workers_raw"] == 0:
        smoothed_int = 0
    decision.update({
        "static_cap": static_cap,
        "kalman_warnings": warn,
        "smoothed_cap": smoothed_int,
        "metrics": metrics,
    })
    return decision


def main(argv: list[str]) -> int:
    decision = evaluate()
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(decision, indent=1))
    except Exception:
        pass
    if "--json" in argv:
        print(json.dumps(decision, indent=1))
    else:
        print(f"target_raw={decision['target_workers_raw']} smoothed={decision['smoothed_cap']} "
              f"min_headroom={decision['min_headroom']} per_dim={decision['per_dimension']} "
              f"warn={decision['kalman_warnings']} reason={decision['reason']}")
    return 0 if decision["can_dispatch"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
