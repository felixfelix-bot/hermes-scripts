#!/usr/bin/env python3
"""opportunity_watch.py — cheaper-lane / subscription opportunity hints (ADR-016 §32.8).

Reads the measured effective $/M per lane (``kalman_pricing.json``) plus probe
health (``provider_probe.json``) and reports, for the daily operator digest:

  * a **reachable** lane materially cheaper than the one we are paying the most
    for, and
  * an **unfunded** lane that would be cheaper if activated.

Writes ``~/.hermes/bot/opportunity_hints.json`` (consumed by the digest). Pure
stdlib so it can run under cron without the proxy.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BOT = Path.home() / ".hermes" / "bot"
PRICING_PATH = BOT / "kalman_pricing.json"
PROBE_PATH = BOT / "provider_probe.json"
OUT_PATH = BOT / "opportunity_hints.json"

DEFAULT_THRESHOLD = 0.7  # flag a lane ≥30% cheaper than what we pay


def find_opportunities(rates: dict, *, paying_rate: float | None,
                       unhealthy: set | None = None,
                       threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """Return lanes materially cheaper than *paying_rate*.

    *rates* maps provider → effective $/M (>0). *unhealthy* providers are
    reported as ``activate`` (not ``switch``) opportunities.
    """
    unhealthy = unhealthy or set()
    out: list[dict] = []
    if not paying_rate or paying_rate <= 0:
        return out
    cutoff = paying_rate * threshold
    for name, rate in rates.items():
        try:
            r = float(rate)
        except (TypeError, ValueError):
            continue
        if r <= 0 or r != r or r == float("inf"):  # skip 0/NaN/inf
            continue
        if r < cutoff:
            out.append({
                "provider": name,
                "rate_per_m": round(r, 6),
                "vs_paying_per_m": round(paying_rate, 6),
                "savings_pct": round((1.0 - r / paying_rate) * 100.0, 1),
                "kind": "activate" if name in unhealthy else "switch",
            })
    out.sort(key=lambda d: d["rate_per_m"])
    return out


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def collect(*, pricing_path: Path | None = None,
            probe_path: Path | None = None,
            out_path: Path | None = None) -> dict:
    pricing = _load(pricing_path or PRICING_PATH)
    provs = pricing.get("providers", pricing) if isinstance(pricing, dict) else {}
    rates: dict[str, float] = {}
    for name, row in (provs or {}).items():
        if isinstance(row, dict):
            r = row.get("effective_rate_per_m")
            if r is None:
                r = row.get("base_rate_per_m")
            try:
                rates[name] = float(r)
            except (TypeError, ValueError):
                pass

    probe = _load(probe_path or PROBE_PATH)
    unhealthy = {n for n, row in probe.items()
                 if isinstance(row, dict) and row.get("healthy") is False}
    # providers we actually pay for = finite, positive rates
    payable = {n: r for n, r in rates.items() if r and r > 0 and r != float("inf")}
    paying_rate = max(payable.values()) if payable else None

    hints = find_opportunities(rates, paying_rate=paying_rate, unhealthy=unhealthy)
    result = {"ts": time.time(), "paying_rate_per_m": paying_rate,
              "opportunities": hints}
    p = out_path or OUT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=1))
    except Exception:
        pass
    return result


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(collect(), indent=2))
