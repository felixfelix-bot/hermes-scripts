#!/usr/bin/env python3
"""fleet_usage.py — shared, private usage telemetry so both fleet nodes run the
SAME Kalman pressure filter and agree (D-128 §2.7.1).

Each node samples its own burn (from the router telemetry `zai_usage.db`) into
  ~/.hermes/state/fleet-usage/usage-<node>.json        (cumulative snapshot)
  ~/.hermes/state/fleet-usage/usage_series-<node>.jsonl (append-only samples)
and pushes those files to the peer via the existing SSH heartbeat transport.
Both nodes then merge {local, peer} and compute `kalman_pressure` from the same
input -> identical predictions. No secrets; private transport only.

Pure functions are unit-tested (tests/test_fleet_usage.py). CLI:
  fleet_usage.py sample [--node N] [--write]
  fleet_usage.py merge  [--json]
  fleet_usage.py predict [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
STATE_DIR = HERMES / "state" / "fleet-usage"
PEERS = BOT / "peers"
DB = BOT / "zai_usage.db"

WINDOW_S = int(os.environ.get("FLEET_USAGE_WINDOW_S", "21600"))   # 6h
BUCKET_S = int(os.environ.get("FLEET_USAGE_BUCKET_S", "300"))     # 5 min
DEFAULT_REF_TPH = float(os.environ.get("FLEET_USAGE_REF_TPH", "2_000_000"))


def _read(p: Path, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def _ts(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def _node() -> str:
    return _read(BOT / "fleet.json", {}).get("node") or os.uname().nodename


# ── sampling ──────────────────────────────────────────────────────────────────

def read_api_calls(db: Path, since: float) -> list[dict]:
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute(
            "select ts, key_name, coalesce(model,''), coalesce(total_tokens,0), "
            "coalesce(status_code,0) from api_calls order by ts desc limit 20000"
        ).fetchall()
        c.close()
    except sqlite3.Error:
        return []
    out = []
    for ts, key, model, tokens, status in rows:
        t = _ts(ts)
        if t >= since:
            out.append({"ts": t, "key_name": key or "?", "model": model,
                        "tokens": int(tokens or 0), "status": status})
    return out


def sample(node: str | None = None, now: float | None = None) -> dict:
    node = node or _node()
    now = now if now is not None else time.time()
    calls = read_api_calls(DB, now - WINDOW_S)
    per_key: dict[str, dict] = {}
    for r in calls:
        k = per_key.setdefault(r["key_name"], {"tokens": 0, "calls": 0})
        k["tokens"] += r["tokens"]
        k["calls"] += 1
    series_row = {"ts": int(now), "node": node,
                  "keys": {k: {"tokens": v["tokens"], "calls": v["calls"],
                               "tph": round(v["tokens"] / (WINDOW_S / 3600.0), 1)}
                           for k, v in per_key.items()}}
    snap = {"node": node, "ts": int(now), "window_s": WINDOW_S,
            "keys": per_key, "total_tokens": sum(v["tokens"] for v in per_key.values()),
            "total_calls": sum(v["calls"] for v in per_key.values())}
    return {"snapshot": snap, "series_row": series_row}


def write_sample(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    node = data["snapshot"]["node"]
    (STATE_DIR / f"usage-{node}.json").write_text(json.dumps(data["snapshot"], indent=1))
    with (STATE_DIR / f"usage_series-{node}.jsonl").open("a") as fh:
        fh.write(json.dumps(data["series_row"]) + "\n")


# ── merge + Kalman ──────────────────────────────────────────────────────────

def load_series(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def merge_series(local_dir: Path = STATE_DIR, peers_dir: Path = PEERS) -> list[dict]:
    paths = list(local_dir.glob("usage_series-*.jsonl"))
    paths += list(peers_dir.glob("usage_series-*.jsonl"))
    return load_series(paths)


def _kalman_2state(xs: list[float], q: float = 1e-3,
                   r: float = 1e-1) -> tuple[float, float]:
    """Simplest 2-state (level, velocity) Kalman; deterministic."""
    if not xs:
        return 0.0, 0.0
    x, v = xs[0], 0.0
    p00, p01, p10, p11 = 1.0, 0.0, 0.0, 1.0
    for z in xs[1:]:
        # predict
        x, v = x + v, v
        p00, p01, p10, p11 = p00 + p01 + p10 + q, p01 + p11, p10 + p11, p11 + q
        # update
        s = p00 + r
        k0, k1 = p00 / s, p10 / s
        y = z - x
        x, v = x + k0 * y, v + k1 * y
        p00, p01, p10, p11 = (1 - k0) * p00, (1 - k0) * p01, p10 - k1 * p00, p11 - k1 * p01
    return x, v


def _bucket(rows: list[dict], bucket_s: int = BUCKET_S) -> dict[tuple[str, str], list[float]]:
    """(node,key) -> ordered per-bucket token counts."""
    out: dict[tuple[str, str], dict[int, float]] = {}
    for row in rows:
        b = int(row.get("ts", 0)) // bucket_s
        for key, v in (row.get("keys") or {}).items():
            out.setdefault((row.get("node", "?"), key), {})
            out[(row.get("node", "?"), key)][b] = float(v.get("tokens", 0))
    res = {}
    for k, buckets in out.items():
        res[k] = [buckets[b] for b in sorted(buckets)]
    return res


def kalman_pressure(rows: list[dict], ref_tph: float = DEFAULT_REF_TPH,
                    bucket_s: int = BUCKET_S) -> dict:
    """Per-(node,key) smoothed burn rate + trend + normalized pressure.
    Deterministic given the same input rows (both nodes agree)."""
    out: dict[str, dict] = {}
    for (node, key), series in _bucket(rows, bucket_s).items():
        # convert per-bucket tokens to tokens/hour
        tph_series = [x * (3600.0 / bucket_s) for x in series]
        level, vel = _kalman_2state(tph_series)
        pressure = min(1.0, level / ref_tph) if ref_tph else 0.0
        out[f"{node}:{key}"] = {
            "node": node, "key_name": key,
            "rate_tph": round(level, 1), "accel_tph2": round(vel, 1),
            "pressure": round(pressure, 4), "samples": len(series),
        }
    return out


def merged_pressure() -> dict:
    return kalman_pressure(merge_series())


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample"); p.add_argument("--node", default="")
    p.add_argument("--write", action="store_true"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("merge"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("predict"); p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "sample":
        data = sample(args.node or None)
        if args.write:
            write_sample(data)
        print(json.dumps(data if args.json else data["snapshot"], indent=1))
        return 0
    if args.cmd == "merge":
        rows = merge_series()
        print(json.dumps(rows if args.json else {"merged_samples": len(rows)}, indent=1))
        return 0
    if args.cmd == "predict":
        preds = merged_pressure()
        print(json.dumps(preds, indent=1))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
