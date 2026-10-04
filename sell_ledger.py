#!/usr/bin/env python3
"""sell_ledger.py — self-charge ledger (ADR-018).

Dress rehearsal for selling tokens on routstr: treat the operator as an
infinite-money customer paying the **exposed sale price**, and record
``actual_tokens × exposed_price`` per request. This yields a notional-revenue
system-health metric and a profitability signal (revenue − accounting cost)
without any real customer.

Exposed price defaults to ``upstream_cost × (1 + MARKUP)`` per model (from the
same ``api_calls`` rows); a configured table can override it. Pure stdlib.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

MARKUP = float(os.environ.get("SELL_MARKUP", "0.30"))
CONFIG_PATH = Path.home() / ".hermes" / "bot" / "sell_pricing.json"
OUT_PATH = Path.home() / ".hermes" / "bot" / "sell_ledger.json"


def load_exposed_overrides(path: Path | None = None) -> dict:
    """Optional {model: exposed_$/M} overrides."""
    p = path or CONFIG_PATH
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def exposed_rate(model: str, upstream_cost_per_m: float,
                 overrides: dict | None = None) -> float:
    """Exposed $/M for *model*: explicit override, else upstream × (1+markup)."""
    ov = overrides or {}
    if model in ov:
        try:
            return float(ov[model])
        except (TypeError, ValueError):
            pass
    return max(0.0, float(upstream_cost_per_m)) * (1.0 + MARKUP)


def build_ledger(db_path: str, window_hours: float = 24.0,
                 now: float | None = None,
                 overrides: dict | None = None) -> dict:
    """Compute the per-model self-charge for the trailing window.

    Revenue = Σ (total_tokens/1e6) × exposed_$/M over rows with a known cost.
    """
    now = now if now is not None else time.time()
    since = now - window_hours * 3600.0
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT COALESCE(model,'?'), SUM(total_tokens) AS tok, "
            "SUM(COALESCE(cost_usd,0)) AS cost "
            "FROM api_calls WHERE ts > ? GROUP BY COALESCE(model,'?')",
            (since,),
        ).fetchall()
    finally:
        con.close()

    per_model = []
    tot_rev = tot_cost = tot_tok = 0
    for model, tok, cost in rows:
        tok = int(tok or 0)
        cost = float(cost or 0.0)
        cost_per_m = (cost / (tok / 1e6)) if tok > 0 else 0.0
        rate = exposed_rate(model, cost_per_m, overrides)
        revenue = (tok / 1e6) * rate
        per_model.append({
            "model": model, "tokens": tok,
            "cost_usd": round(cost, 6),
            "upstream_per_m": round(cost_per_m, 6),
            "exposed_per_m": round(rate, 6),
            "revenue_usd": round(revenue, 6),
            "margin_usd": round(revenue - cost, 6),
        })
        tot_rev += revenue
        tot_cost += cost
        tot_tok += tok

    return {
        "ts": now,
        "window_hours": window_hours,
        "markup": MARKUP,
        "totals": {
            "tokens": tot_tok,
            "revenue_usd": round(tot_rev, 6),
            "cost_usd": round(tot_cost, 6),
            "margin_usd": round(tot_rev - tot_cost, 6),
            "margin_pct": round((tot_rev - tot_cost) / tot_rev * 100.0, 2) if tot_rev else 0.0,
        },
        "models": sorted(per_model, key=lambda r: -r["revenue_usd"]),
    }


def write_summary(db_path: str, window_hours: float = 24.0,
                  out_path: Path | None = None) -> dict:
    led = build_ledger(db_path, window_hours=window_hours,
                       overrides=load_exposed_overrides())
    p = out_path or OUT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(led, indent=1))
    except Exception:
        pass
    return led


if __name__ == "__main__":  # pragma: no cover
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.hermes/bot/zai_usage.db")
    wh = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    print(json.dumps(write_summary(db, wh), indent=2))
