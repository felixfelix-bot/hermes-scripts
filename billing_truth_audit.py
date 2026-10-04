#!/usr/bin/env python3
"""billing_truth_audit.py — F5 truth audit (PLAN Layer 2, 2026-09-27).

INVARIANT (stated so it can be FALSE)
-------------------------------------
The vendor's billing PLAN TIER, BILLING MODE, INCLUDED QUOTA and OVERAGE RATE
are UNCHANGED since the last recorded observation:

    record(vendor, today)  ==  reference_state        (core keys)

and the MEASURED cache ratio / effective $/M / kWh-per-billion are recorded so
a vendor-driven cost move can never happen silently.

WHY IT EXISTS
-------------
2026-09-27: plan tier, billing mode (energy vs token), included kWh, overage
rate and the achieved cache ratio were recorded NOWHERE — a plan decision could
only be answered by hand-querying the vendor API. A plan/mode/quota change is
exactly how a vendor-driven cost move happens with no alarm (energy vs token is
~2x on the NW lane: measured $184.52/mo energy vs ~$94/mo token).

TRUTH SOURCE — the VENDOR's own endpoints
-----------------------------------------
  NeuralWatt GET /v1/quota         (plan, accounting_method, kwh_included,
                                    kwh_used, kwh_remaining, overage, period)
  NeuralWatt GET /v1/usage/summary (totals.total_cost_usd, totals.total_tokens,
                                    totals.cached_tokens, energy_kwh_charged)
Field paths and the auth env-var NAME come from
``state/fleet/vendor_truth_sources.json`` — never hardcoded here.

BASELINE
--------
The reference is, in order: an explicit ``--reference-file``, else the last
recorded state file, else the shipped ``state/fleet/billing_truth_baseline.json``
(the numbers observed on 2026-09-27). A first run with no reference just
RECORDS (exit 0). ``--as-of DATE`` replays against history.

CONTRACT
--------
* SILENT + exit 0 when every core key matches the reference (no stdout).
* On change: print the before/after values and exit 2.
* exit 1 when the vendor truth cannot be read.

USAGE
-----
  billing_truth_audit.py [--config PATH] [--vendor-file PATH]
                         [--state-file PATH] [--reference-file PATH]
                         [--as-of DATE] [--live] [--no-write] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
CORE_KEYS = ["plan", "billing_mode", "kwh_included", "overage_rate_usd_per_kwh"]
ENV_FILES = [
    Path.home() / ".hermes" / "profiles" / "manager" / ".env",
    Path.home() / ".hermes" / ".env",
    Path.home() / ".hermes" / "bot" / ".env",
]


def discover_repo(start: Path | None = None) -> Path | None:
    p = (start or HERE).resolve()
    if p.is_file():
        p = p.parent
    for _ in range(12):
        if ((p / "state" / "fleet" / "vendor_truth_sources.json").exists()
                or (p / "scripts" / "engine" / "flat_router.py").exists()):
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def resolve_config(explicit: str | None) -> Path | None:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("VENDOR_TRUTH_SOURCES")
    if env:
        cands.append(Path(env).expanduser())
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    cands += [home / "state" / "fleet" / "vendor_truth_sources.json",
              Path.home() / ".hermes" / "state" / "fleet" / "vendor_truth_sources.json"]
    repo = discover_repo()
    if repo:
        cands.append(repo / "state" / "fleet" / "vendor_truth_sources.json")
    for c in cands:
        if c.is_file():
            return c
    return None


def find_repo(config_path: Path | None = None) -> Path | None:
    """Locate the repo. See price_truth_audit.find_repo."""
    cands = []
    env = os.environ.get("TRUTH_AUDIT_REPO")
    if env:
        cands.append(Path(env).expanduser())
    cands += [Path.home() / "hermes-orchestration",
              Path.home() / ".hermes" / "orchestration",
              Path.cwd()]
    if config_path is not None:
        cands.append(config_path.parent.parent.parent)
    for c in cands:
        r = discover_repo(c)
        if r is not None:
            return r
    return None


def get_path(obj, dotted, default=None):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def load_env() -> dict:
    env = dict(os.environ)
    for f in ENV_FILES:
        try:
            for line in f.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#", 1)[0].strip().strip("'").strip('"')
                env.setdefault(k.strip(), v)
        except OSError:
            continue
    return env


def _http_get(url: str, key: str, timeout: int = 25) -> dict:
    headers = {"User-Agent": "hermes-billing-truth-audit/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())


def fetch_vendor(cfg: dict, provider: str, env: dict) -> dict:
    pcfg = (cfg.get("providers") or {}).get(provider) or {}
    base = pcfg["base_url"].rstrip("/")
    key = next((env.get(n) for n in pcfg.get("auth_env", []) if env.get(n)), "")
    blob: dict = {}
    for ep_name, ep in (pcfg.get("endpoints") or {}).items():
        if ep_name not in ("quota", "usage_summary"):
            continue
        url = base + "/" + ep["path"].lstrip("/")
        try:
            blob[ep_name] = _http_get(url, key)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError) as exc:
            raise RuntimeError(f"vendor fetch failed for {provider}.{ep_name} "
                               f"({url}): {type(exc).__name__}") from exc
    return blob


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_record(cfg: dict, provider: str, blob: dict,
                 fallback_overage: float | None = None) -> dict:
    pcfg = (cfg.get("providers") or {}).get(provider) or {}
    eps = pcfg.get("endpoints") or {}
    q = blob.get("quota") or {}
    u = blob.get("usage_summary") or {}
    qf = (eps.get("quota") or {}).get("fields") or {}
    uf = (eps.get("usage_summary") or {}).get("fields") or {}

    cost = _f(get_path(u, uf.get("total_cost_usd", "totals.total_cost_usd")))
    toks = _f(get_path(u, uf.get("total_tokens", "totals.total_tokens")))
    prompt = _f(get_path(u, uf.get("prompt_tokens", "totals.prompt_tokens")))
    cached = _f(get_path(u, uf.get("cached_tokens", "totals.cached_tokens")))
    kwh_charged = _f(get_path(u, uf.get("energy_kwh_charged",
                                       "totals.energy_kwh_charged")))

    overage = _f(get_path(q, qf.get("overage_rate_usd_per_kwh",
                                    "limits.overage_rate_usd_per_kwh")))
    if overage is None:
        overage = fallback_overage

    rec = {
        "plan": get_path(q, qf.get("plan", "subscription.plan")),
        "status": get_path(q, qf.get("status", "subscription.status")),
        "billing_mode": (get_path(q, qf.get("billing_mode", "balance.accounting_method"))
                         or get_path(u, uf.get("billing_mode", "accounting_method"))),
        "kwh_included": _f(get_path(q, qf.get("kwh_included", "subscription.kwh_included"))),
        "kwh_used": _f(get_path(q, qf.get("kwh_used", "subscription.kwh_used"))),
        "kwh_remaining": _f(get_path(q, qf.get("kwh_remaining",
                                               "subscription.kwh_remaining"))),
        "in_overage": get_path(q, qf.get("in_overage", "subscription.in_overage")),
        "kwh_reset_date": get_path(q, qf.get("kwh_reset_date",
                                             "subscription.kwh_reset_date")),
        "credits_remaining_usd": _f(get_path(q, qf.get("credits_remaining_usd",
                                                        "balance.credits_remaining_usd"))),
        "overage_rate_usd_per_kwh": overage,
    }
    # measured (recorded, informational — a plan/quota change is the alert)
    rec["cache_ratio"] = round(cached / prompt, 4) if (cached and prompt) else None
    rec["effective_usd_per_M"] = (round(cost / (toks / 1e6), 6)
                                  if (cost and toks) else None)
    rec["kwh_per_billion_tokens"] = (round(kwh_charged / (toks / 1e9), 4)
                                     if (kwh_charged and toks) else None)
    return rec


def diff_core(reference: dict, current: dict) -> list[dict]:
    """Core-key changes only (numeric tolerance 1e-6-ish; strings exact)."""
    out = []
    for k in CORE_KEYS:
        rv, cv = reference.get(k), current.get(k)
        if rv is None or cv is None:
            if (rv is None) != (cv is None):
                out.append({"key": k, "reference": rv, "current": cv})
            continue
        if isinstance(rv, (int, float)) and isinstance(cv, (int, float)):
            if abs(float(rv) - float(cv)) > max(1e-9, abs(float(rv)) * 1e-6):
                out.append({"key": k, "reference": rv, "current": cv})
        elif str(rv) != str(cv):
            out.append({"key": k, "reference": rv, "current": cv})
    return out


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="F5 vendor billing truth audit")
    ap.add_argument("--config", default=None)
    ap.add_argument("--vendor-file", default=None)
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--reference-file", default=None)
    ap.add_argument("--provider", default="neuralwatt")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    config_path = resolve_config(args.config)
    if config_path is None:
        print("[billing] cannot locate vendor_truth_sources.json", file=sys.stderr)
        return 1
    cfg = json.loads(config_path.read_text())
    repo = find_repo(config_path) or config_path.parent.parent.parent

    state_file = Path(args.state_file).expanduser() if args.state_file else Path(
        os.environ.get("BILLING_TRUTH_STATE",
                       str(Path.home() / ".hermes" / "bot"
                           / "billing_truth_state.json"))).expanduser()

    # vendor payload
    vendor = None
    if args.vendor_file:
        vendor = json.loads(Path(args.vendor_file).expanduser().read_text())
    elif args.as_of:
        snap = repo / "state" / "fleet" / "truth_snapshots" / f"billing-{args.as_of}.json"
        if snap.is_file():
            vendor = json.loads(snap.read_text())
    if vendor is None:
        try:
            vendor = fetch_vendor(cfg, args.provider, load_env())
        except RuntimeError as exc:
            print(f"[billing] {exc}", file=sys.stderr)
            return 1
    if args.provider in vendor:
        blob = vendor[args.provider]
    else:
        blob = vendor

    # reference
    reference = None
    if args.reference_file:
        rf = json.loads(Path(args.reference_file).expanduser().read_text())
        reference = rf.get("billing", rf)
    elif state_file.is_file():
        try:
            reference = json.loads(state_file.read_text()).get("billing")
        except (json.JSONDecodeError, OSError):
            reference = None
    if reference is None:
        base = repo / "state" / "fleet" / "billing_truth_baseline.json"
        if base.is_file():
            reference = json.loads(base.read_text()).get("billing")

    current = build_record(cfg, args.provider, blob,
                           fallback_overage=(reference or {}).get(
                               "overage_rate_usd_per_kwh"))

    if not args.no_write:
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(
                {"provider": args.provider, "billing": current}, indent=2,
                sort_keys=True))
        except OSError as exc:
            print(f"[billing] cannot write state {state_file}: {exc}",
                  file=sys.stderr)

    changes = diff_core(reference, current) if reference else []
    if not changes:
        return 0
    if args.json:
        print(json.dumps({"provider": args.provider, "changes": changes,
                          "current": current}, indent=2, sort_keys=True))
    else:
        for c in changes:
            print(f"[billing] {args.provider} {c['key']} CHANGED: "
                  f"{c['reference']!r} -> {c['current']!r}")
        print(f"[billing] vendor billing plan/quota changed on {args.provider} "
              f"({len(changes)} core key(s)) — re-price the lane")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
