#!/usr/bin/env python3
"""funding_price_watch.py — surface when underfunding raises the price (D-140).

The flat router prices every lane from the Kalman filters and routes on price.
When a lane 402s it is priced to +inf (D-138) and the router falls back to a
deliverable-but-possibly-pricier lane. That is correct routing, but the operator
should know when a *cheaper* lane is merely **unfunded** — that is a decision
they can reverse by topping up.

This watch compares:
  * lanes seen unfunded recently (``~/.hermes/bot/funding_events.jsonl``),
  * their nominal seed price (``flat_router._SEED_RATES``, $/M), against
  * what we are actually paying now (``measured_rates`` / recent ``api_calls``).

If an unfunded lane is ≥ ``SAVINGS_RATIO``× cheaper than the cheapest lane we
are currently paying for, it emits an operator alert (rate-limited).

Usage:
  funding_price_watch.py [--dry-run] [--json] [--window-h 24] [--min-tokens 1000]
Exit: 0 (report-only; never fails the cron).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = Path(os.path.expanduser("~/.hermes/bot"))     # canonical (router's dir)
USAGE_DB = BOT / "zai_usage.db"
FUNDING_EVENTS = Path(os.path.expanduser("~/.hermes/bot/funding_events.jsonl"))
STATE = Path(os.path.expanduser("~/.hermes/bot/funding_price_state.json"))
ALERTS_CFG = BOT / "alerts_channel.json"
NSEC = HERMES / "keys" / "hermes-ops" / "cobrador.nsec"
NSEC_FALLBACK = Path(os.path.expanduser("~/.hermes/keys/hermes-ops/cobrador.nsec"))

SAVINGS_RATIO = 2.0      # alert when an unfunded lane is >=2x cheaper
COOLDOWN_S = 6 * 3600    # per-provider re-alert window
TOPUP_HORIZON_H = 24.0   # alert when a lane is projected to run dry within a day
# A seed/served ratio this large is not a routing fact, it is a unit mismatch
# (F2: "served >= seed*2.0" was false by 125x). Report it instead of staying silent.
UNIT_SUSPECT_RATIO = 100.0
BLIND_COOLDOWN_S = 24 * 3600  # re-report a blind sensor at most once a day


def _near_exhaustion(horizon_h: float) -> dict:
    """provider -> projected hours-to-exhaust for lanes running dry soon.

    Reads the router's OWN kalman_samples predictions (the same source as
    `exhaust_weight`) so a top-up can be arranged BEFORE the lane 402s and the
    router prices it to +inf. Never raises.
    """
    out: dict = {}
    try:
        import sqlite3 as _sq
        c = _sq.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=5)
        c.row_factory = _sq.Row
        rows = c.execute(
            "SELECT key, ts, exhausts_in_hours, will_exhaust FROM kalman_samples "
            "ORDER BY ts DESC LIMIT 500").fetchall()
        c.close()
    except Exception:
        return out
    by_key: dict = {}
    for r in rows:
        try:
            by_key.setdefault(r["key"], []).append(r)
        except Exception:
            continue
    for key, rs in by_key.items():
        ts = rs[0]["ts"]
        latest = [r for r in rs if r["ts"] == ts]
        exh = [r["exhausts_in_hours"] for r in latest
               if r["will_exhaust"] and r["exhausts_in_hours"] is not None]
        if exh and min(exh) <= horizon_h:
            out[key] = round(min(exh), 1)
    return out


def _seed_rates() -> dict:
    """Load flat_router._SEED_RATES (nominal $/M) without importing the server."""
    for base in (Path(os.path.expanduser("~/.hermes/bot")),
                 Path(__file__).resolve().parent.parent / "engine"):
        p = base / "flat_router.py"
        if not p.exists():
            continue
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_fr_seed", p)
            m = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(base))
            spec.loader.exec_module(m)
            return dict(getattr(m, "_SEED_RATES", {}) or {})
        except Exception:
            continue
    return {}


def _sensor_health(window_h: float) -> dict:
    """How much sensor input did the 402 file actually produce (PLAN §2.3)?

    F2 as measured: this watch was green for 598 consecutive runs with an empty
    state file, because its only unfunded-lane sensor is a 402 written when a
    lane is *dialled* — and the disabled-lane flag stops the dialling. A sensor
    may never be the thing it watches, so the watch now reports the absence of
    its own input: silence is only meaningful if the sensor was alive.

    Read-only, never raises.
    """
    health = {"file_present": FUNDING_EVENTS.exists(), "events_total": 0,
              "events_in_window": 0, "last_event_age_h": None}
    try:
        lines = FUNDING_EVENTS.read_text().splitlines()
    except OSError:
        return health
    cutoff = time.time() - window_h * 3600
    newest = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            ts = float(e.get("ts", 0) or 0)
        except Exception:
            continue
        health["events_total"] += 1
        newest = max(newest, ts)
        if ts >= cutoff:
            health["events_in_window"] += 1
    if newest:
        health["last_event_age_h"] = round((time.time() - newest) / 3600.0, 1)
    return health


def _unfunded(window_h: float) -> dict:
    """provider -> latest 402 ts within the window."""
    out: dict = {}
    cutoff = time.time() - window_h * 3600
    try:
        for line in FUNDING_EVENTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = float(e.get("ts", 0) or 0)
            if ts >= cutoff:
                p = e.get("provider")
                if p:
                    out[p] = max(out.get(p, 0), ts)
    except OSError:
        pass
    return out


def _served_rates(window_h: float, min_tokens: int) -> dict:
    """key_name -> $/M actually paid over the window (needs cost + tokens)."""
    rates: dict = {}
    if not USAGE_DB.exists():
        return rates
    cutoff = time.time() - window_h * 3600
    try:
        c = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True)
        rows = c.execute(
            "select key_name, sum(coalesce(cost_usd,0)) s, "
            "sum(coalesce(total_tokens,0)) t from api_calls "
            "where ts>=? and cost_usd is not null and total_tokens>0 "
            "group by key_name", (cutoff,)).fetchall()
        c.close()
        for name, s, t in rows:
            if name and t and t >= min_tokens and s and s > 0:
                rates[name] = (float(s) / float(t)) * 1e6
    except Exception:
        pass
    return rates


def _nak() -> str:
    for c in (os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak",
              "/usr/bin/nak"):
        if Path(c).exists():
            return c
    return "nak"


def _post_buzz(text: str) -> bool:
    cfg = {}
    try:
        cfg = json.loads(ALERTS_CFG.read_text())
    except Exception:
        return False
    group = cfg.get("orange_group")
    nsec = NSEC if NSEC.exists() else NSEC_FALLBACK
    if not group or not nsec.exists():
        return False
    try:
        sec = nsec.read_text().strip()
        r = subprocess.run(
            [_nak(), "event", "-k", "9", "-t", f"h={group}",
             "-t", "client=hermes-fleet", "-t", "t=funding-hint",
             "-c", text, "--auth", "--sec", sec,
             cfg.get("relay", "wss://relay.orangesync.tech")],
            capture_output=True, text=True, timeout=60)
        return "success" in (r.stdout + r.stderr)
    except Exception:
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-h", type=float, default=24.0)
    ap.add_argument("--min-tokens", type=int, default=1000)
    ap.add_argument("--topup-horizon-h", type=float, default=TOPUP_HORIZON_H,
                    help="alert for lanes projected to exhaust within this many "
                         "hours (0 disables the preemptive top-up hint)")
    args = ap.parse_args(argv)

    seeds = _seed_rates()
    unfunded = _unfunded(args.window_h)
    served = _served_rates(args.window_h, args.min_tokens)

    hints = []
    unit_suspect = []
    if unfunded and served:
        cheapest_served = min(served.items(), key=lambda kv: kv[1])
        served_name, served_rate = cheapest_served
        for prov, _ts in unfunded.items():
            seed = seeds.get(prov)
            if seed is None or seed <= 0:
                continue
            if served_rate > 0 and served_rate >= seed * SAVINGS_RATIO:
                hints.append({
                    "unfunded": prov, "unfunded_seed_usd_per_m": round(seed, 4),
                    "served_via": served_name,
                    "served_usd_per_m": round(served_rate, 4),
                    "savings_usd_per_m": round(served_rate - seed, 4),
                })
            elif served_rate > 0 and max(seed / served_rate,
                                         served_rate / seed) >= UNIT_SUSPECT_RATIO:
                # A silent false negative is the F2 pattern: the `>= seed*2` test
                # fails because the two numbers are not the same unit (measured:
                # served 0.0354 vs seed*2 4.42 — false by 125x, green for ever).
                # Say so, loudly, instead of reporting "no hint" for ever.
                hi, lo = max(seed, served_rate), min(seed, served_rate)
                unit_suspect.append({
                    "provider": prov,
                    "unfunded_seed_usd_per_m": round(seed, 4),
                    "served_via": served_name,
                    "served_usd_per_m": round(served_rate, 4),
                    "ratio": round(hi / lo, 1),
                    "direction": "seed >> served" if seed > served_rate
                                 else "served >> seed",
                })

    state = {}
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    now = time.time()
    fired = []

    def _emit(key: str, msg: str, cooldown: float) -> bool:
        last = float((state.get(key) or {}).get("last_alert", 0) or 0)
        if now - last < cooldown:
            print(f"(rate-limited: {key})")
            return False
        if args.dry_run:
            print("DRY:", msg)
        else:
            ok = _post_buzz(msg)
            print(("posted:" if ok else "post-failed:") + " " + msg)
        state[key] = {"last_alert": now}
        return True

    for h in hints:
        prov = h["unfunded"]
        if _emit(prov, (f"[FUNDING? · info] Unfunded lane '{prov}' "
                        f"(~${h['unfunded_seed_usd_per_m']}/M) is "
                        f"≥{SAVINGS_RATIO:g}× cheaper than the lane we're paying "
                        f"({h['served_via']} ~${h['served_usd_per_m']}/M). Funding "
                        f"{prov} would save ~${h['savings_usd_per_m']}/M. "
                        f"(router already routes around it)"), COOLDOWN_S):
            state[prov] = {"last_alert": now, **h}
            fired.append(h)

    # A ratio above UNIT_SUSPECT_RATIO is not a routing fact: the two numbers are
    # not the same unit (F2). Report it — a silent false negative is the defect.
    unit_fired = []
    for u in unit_suspect:
        key = f"unit:{u['provider']}"
        if _emit(key, (f"[UNIT? · info] funding-price-watch: lane '{u['provider']}' "
                       f"seed ${u['unfunded_seed_usd_per_m']}/M vs measured served "
                       f"${u['served_usd_per_m']}/M ({u['ratio']}x, {u['direction']}) "
                       f"— the >={SAVINGS_RATIO:g}x savings test cannot be evaluated "
                       f"in these units, so its silence means nothing. Verify the seed "
                       f"rate units for '{u['provider']}'."), COOLDOWN_S):
            unit_fired.append(u)

    # D-138/D-140: preemptive top-up — a lane predicted to run dry within the
    # horizon. Alert BEFORE the 402 so the operator can fund it and keep the
    # cheap lane in the market (the router prices it +inf once dry).
    near = _near_exhaustion(args.topup_horizon_h) if args.topup_horizon_h > 0 else {}
    topup_fired = []
    for prov, hrs in sorted(near.items(), key=lambda kv: kv[1]):
        k = f"topup:{prov}"
        last = float((state.get(k) or {}).get("last_alert", 0) or 0)
        if now - last < COOLDOWN_S:
            continue
        msg = (f"[TOPUP · info] Lane '{prov}' is projected to exhaust in ~{hrs}h "
               f"(< {args.topup_horizon_h:g}h). Top it up to keep the cheap lane "
               f"in the market — once dry the router prices it +inf (D-138).")
        if args.dry_run:
            print("DRY:", msg)
        else:
            ok = _post_buzz(msg)
            print(("posted:" if ok else "post-failed:") + " " + msg)
        state[k] = {"last_alert": now, "exhausts_in_hours": hrs}
        topup_fired.append({"provider": prov, "exhausts_in_hours": hrs})

    # ---- sensor health (PLAN §2.3): silence is only meaningful if the sensor was
    # alive. F2's watch was green 598 times because its input could never exist.
    sensor = _sensor_health(args.window_h)
    blindness = None
    if sensor["events_in_window"] == 0 and served:
        blindness = (
            f"[BLIND · info] funding-price-watch: the unfunded-lane sensor produced "
            f"{sensor['events_total']} event(s) in its entire history and none in the "
            f"last {args.window_h:g}h (file present: {sensor['file_present']}) while "
            f"{len(served)} lane(s) served traffic — the D-140 'cheaper unfunded lane' "
            f"alarm CANNOT fire in this state. Its silence is not a green.")
        _emit("blind:funding-events", blindness, BLIND_COOLDOWN_S)

    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))

    if args.json:
        print(json.dumps({"unfunded": sorted(unfunded), "served_rates": served,
                          "hints": hints, "fired": fired,
                          "sensor_health": sensor, "blindness": blindness,
                          "unit_suspect": unit_suspect, "unit_fired": unit_fired,
                          "near_exhaustion": near, "topup_fired": topup_fired},
                         indent=1))
    # Watchdog pattern: silent when consistent. The operator hears about a signal
    # or about blindness — never about a no-op.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
