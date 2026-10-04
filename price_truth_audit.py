#!/usr/bin/env python3
"""price_truth_audit.py — F1 truth audit (PLAN Layer 2, 2026-09-27).

INVARIANT (stated so it can be FALSE)
-------------------------------------
For every (provider, model) lane we route to that has a vendor truth source:

    |our_rate - vendor_rate| / vendor_rate < PRICE_TOLERANCE   (default 0.05)

where
  * ``vendor_rate`` is read from the VENDOR's own endpoint — never from us:
      - per-model published card  (NeuralWatt GET /v1/models,
        OpenRouter GET /api/v1/models), and
      - the account-REALIZED effective $/M  (NeuralWatt GET /v1/usage/summary:
        ``totals.total_cost_usd`` / ``totals.total_tokens``) for energy-billed
        lanes, where the published token card is NOT the billing truth.
  * ``our_rate`` is OUR CLAIM: the router's rate table
      - ``flat_router._SEED_RATES`` (provider-level seed), and
      - ``zai_proxy._OPENROUTER_MODEL_RATES`` / ``zai_proxy.NEURALWATT_RATES``
        (per-model), and
      - the converged rate the proxy logs (``price_observations`` in
        ``zai_usage.db``) when it is newer than the seed.

The audit NEVER compares our number to our number. It read the vendor to find
out whether we are wrong.

WHY IT EXISTS
-------------
2026-09-27: the router converged NeuralWatt at **$0.003807/M** while the vendor
ledger billed **$0.0697/M** ($184.54 / 2,647.8 Mtok) — an **18x** belief, silent
for weeks, that drained the prepaid pool. No alarm compared our belief to the
vendor's published truth. This audit is that control. ``--as-of`` replays it.

CONTRACT
--------
* SILENT + exit 0 when every comparison is within tolerance (no stdout).
* On violation: print a short operator-readable finding (both numbers, ratio,
  delta %, money at stake) for every violating lane and exit 2.
* exit 1 when a truth source could not be fetched/evaluated (fail loud, not
  silent-green — a void run is not a pass).
* Never print, log or attest a key VALUE. Auth is by env-var NAME only.

USAGE
-----
  price_truth_audit.py [--config PATH] [--vendor-file PATH] [--claim-file PATH]
                       [--as-of DATE] [--live] [--json] [--verbose]

  --vendor-file PATH   inject a vendor payload (hermetic tests / replay).
                       Shape: {"<provider>": {"models": {...}|[..],
                                "quota": {...}, "usage_summary": {...}}}
  --claim-file PATH    inject OUR claim (hermetic tests).
                       Shape: {"provider_effective": {prov: usd_per_M},
                               "provider_seed": {prov: usd_per_M},
                               "per_model": {prov: {id: {input,output,..}}}}
  --as-of DATE         replay against history: use the recorded snapshots for
                       DATE (state/fleet/truth_snapshots/price-<DATE>.json) and
                       restrict DB reads to measured_at <= DATE. Proves the
                       audit WOULD have fired on the incident date.
  --live               fetch the vendor endpoints now (default when neither
                       --vendor-file nor a snapshot for --as-of is present).
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
DEFAULT_TOLERANCE = 0.05
ENV_FILES = [
    Path.home() / ".hermes" / "profiles" / "manager" / ".env",
    Path.home() / ".hermes" / ".env",
    Path.home() / ".hermes" / "bot" / ".env",
]


# ──────────────────────────────────────────────────────────────────────────
# path / config resolution
# ──────────────────────────────────────────────────────────────────────────
def discover_repo(start: Path | None = None) -> Path | None:
    """Walk up from this file (or a start path) to the repo root."""
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
    cands += [
        home / "state" / "fleet" / "vendor_truth_sources.json",
        Path.home() / ".hermes" / "state" / "fleet" / "vendor_truth_sources.json",
    ]
    repo = discover_repo()
    if repo:
        cands.append(repo / "state" / "fleet" / "vendor_truth_sources.json")
    for c in cands:
        if c.is_file():
            return c
    return None


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def repo_root(config_path: Path) -> Path:
    # config_path = <repo>/state/fleet/vendor_truth_sources.json
    return config_path.parent.parent.parent


def find_repo(config_path: Path | None = None) -> Path | None:
    """Locate the repo (for the CLAIM side: flat_router / zai_proxy sources).

    The cron runs the INSTALLED copy from ~/.hermes/scripts, so the repo is not
    on the walk-up path from the script. Candidates, in order: an explicit
    ``$TRUTH_AUDIT_REPO`` (set by role 53 in the wrapper), the standard checkout
    locations, the config's own grandparent, and the current working directory.
    """
    cands = []
    env = os.environ.get("TRUTH_AUDIT_REPO")
    if env:
        cands.append(Path(env).expanduser())
    cands += [Path.home() / "hermes-orchestration",
              Path.home() / ".hermes" / "orchestration",
              Path.cwd()]
    if config_path is not None:
        cands.append(repo_root(config_path))
    for c in cands:
        r = discover_repo(c)
        if r is not None:
            return r
    return None


def get_path(obj, dotted: str, default=None):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# ──────────────────────────────────────────────────────────────────────────
# env / http
# ──────────────────────────────────────────────────────────────────────────
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
    headers = {"User-Agent": "hermes-price-truth-audit/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())


def fetch_vendor(cfg: dict, env: dict) -> dict:
    """Return {provider: {"models": ..., "quota": ..., "usage_summary": ...}}."""
    out: dict = {}
    for prov, pcfg in (cfg.get("providers") or {}).items():
        base = pcfg["base_url"].rstrip("/")
        key = next((env.get(n) for n in pcfg.get("auth_env", []) if env.get(n)), "")
        blob: dict = {}
        for ep_name, ep in (pcfg.get("endpoints") or {}).items():
            url = base + "/" + ep["path"].lstrip("/")
            try:
                blob[ep_name] = _http_get(url, key)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, ValueError) as exc:
                raise RuntimeError(
                    f"vendor fetch failed for {prov}.{ep_name} ({url}): "
                    f"{type(exc).__name__}") from exc
        out[prov] = blob
    return out


# ──────────────────────────────────────────────────────────────────────────
# OUR CLAIM — read from the repo source WITHOUT executing it (AST), so the
# cheap cron never imports the proxy / opens sockets / starts threads.
# ──────────────────────────────────────────────────────────────────────────
def literal_dict_from_source(path: Path, symbol: str) -> dict:
    """Extract a dict literal assigned to ``symbol`` WITHOUT importing the
    module (so the cron never executes the proxy: no sockets, no threads).
    Handles both ``X = {...}`` and annotated ``X: t = {...}``."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == symbol:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
    return {}


def latest_price_observations(db: Path, as_of: float | None) -> dict:
    """provider -> (rate_per_m, source) newest row (optionally <= as_of)."""
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        q = ("SELECT provider, rate_per_m, source, ts FROM price_observations "
             "WHERE is_measured = 1")
        args: tuple = ()
        if as_of is not None:
            q += " AND ts <= ?"
            args = (as_of,)
        q += " ORDER BY ts ASC"
        for row in conn.execute(q, args):
            out[row["provider"]] = (row["rate_per_m"], row["source"])
        conn.close()
    except sqlite3.Error:
        return {}
    return out


def load_claims(cfg: dict, config_path: Path, claim_file: str | None,
                as_of: float | None) -> dict:
    if claim_file:
        return json.loads(Path(claim_file).expanduser().read_text())

    repo = find_repo(config_path)
    if repo is None:
        repo = repo_root(config_path)
    claims_cfg = cfg.get("claims") or {}
    seed_sym = claims_cfg.get("seed_rates_symbol", "_SEED_RATES")
    seed_mod = repo / claims_cfg.get("seed_rates_module",
                                     "scripts/engine/flat_router.py")
    per_mod = repo / claims_cfg.get("per_model_rates_module",
                                    "scripts/engine/zai_proxy.py")
    reg_sym = (claims_cfg.get("router_registry_symbols") or ["PROVIDER_MODELS"])

    provider_seed = literal_dict_from_source(seed_mod, seed_sym) if seed_mod.is_file() else {}
    per_model: dict = {}
    for spec in (claims_cfg.get("per_model_rates") or []):
        sym, prov = spec.get("symbol"), spec.get("provider")
        if not sym or not prov:
            continue
        table = literal_dict_from_source(per_mod, sym) if per_mod.is_file() else {}
        per_model.setdefault(prov, {}).update(table)
    provider_model: dict = {}
    for spec in (claims_cfg.get("provider_model_rates") or []):
        sym, prov = spec.get("symbol"), spec.get("provider")
        if not sym or not prov:
            continue
        table = literal_dict_from_source(per_mod, sym) if per_mod.is_file() else {}
        provider_model.setdefault(prov, {}).update(table)
    registry = {s: literal_dict_from_source(seed_mod, s) for s in reg_sym}

    db = Path(str(claims_cfg.get("measured_rates_db",
                                 "~/.hermes/bot/zai_usage.db"))).expanduser()
    observed = latest_price_observations(db, as_of)

    provider_effective = {}
    for prov, (rate, _src) in observed.items():
        if rate is not None:
            provider_effective[prov] = rate
    return {
        "provider_seed": provider_seed,
        "provider_model": provider_model,
        "per_model": per_model,
        "provider_effective": provider_effective,   # converged (logged) rates
        "registry": registry,
    }


# ──────────────────────────────────────────────────────────────────────────
# comparison
# ──────────────────────────────────────────────────────────────────────────
def _vendor_models(cfg: dict, prov: str, blob: dict) -> dict:
    """id -> {input,cached_input,output} (per M) from a vendor /models payload."""
    pcfg = (cfg.get("providers") or {}).get(prov) or {}
    ep = ((pcfg.get("endpoints") or {}).get("models")) or {}
    payload = blob.get("models")
    if payload is None:
        return {}
    rows = get_path(payload, ep.get("list_path", "data"), payload)
    if isinstance(rows, dict):
        rows = list(rows.values())
    scale = ep.get("rate_scale", 1.0)
    id_path = ep.get("id_path", "id")
    ppaths = ep.get("pricing_paths") or {}
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = get_path(row, id_path)
        if not mid:
            continue
        card = {}
        for comp, path in ppaths.items():
            v = get_path(row, path)
            if v is None:
                # OpenRouter carries per-token strings; coerce.
                continue
            try:
                card[comp] = float(v) * scale
            except (TypeError, ValueError):
                continue
        if card:
            out[str(mid)] = card
    return out


def _pct(our: float, vendor: float) -> float:
    if vendor == 0:
        return float("inf")
    return abs(our - vendor) / abs(vendor)


def compute_findings(cfg: dict, claims: dict, vendor: dict,
                     tolerance: float) -> list[dict]:
    findings: list[dict] = []
    providers = cfg.get("providers") or {}

    for prov, pcfg in providers.items():
        blob = vendor.get(prov) or {}
        kind = pcfg.get("pricing_kind", "token")

        # (1) per-model published card vs our per-model claim (token lanes).
        vcard = _vendor_models(cfg, prov, blob)
        our_tables = dict((claims.get("per_model") or {}).get(prov) or {})
        if kind == "energy":
            # provider-native id table (NEURALWATT_RATES) is our per-model claim
            our_tables.update((claims.get("provider_model") or {}).get(prov) or {})

        if kind == "token":
            for mid, our_card in our_tables.items():
                vc = vcard.get(mid)
                if not vc:
                    continue
                for comp in ("input", "output", "cached_input"):
                    if comp not in our_card or comp not in vc:
                        continue
                    our_v, ven_v = float(our_card[comp]), float(vc[comp])
                    # skip sentinels / unusable rows (OpenRouter uses negative
                    # values for "unavailable" and 0 for free/unknown tiers)
                    if ven_v <= 0 or our_v <= 0:
                        continue
                    if _pct(our_v, ven_v) >= tolerance:
                        findings.append({
                            "kind": "per_model_card", "provider": prov,
                            "model": mid, "component": comp,
                            "our": our_v, "vendor": ven_v,
                            "ratio": (our_v / ven_v) if ven_v else None,
                            "delta_pct": _pct(our_v, ven_v) * 100.0,
                        })

        # (2) account-REALIZED effective $/M vs our lane belief (energy lanes).
        if kind == "energy":
            us = blob.get("usage_summary") or {}
            f = ((pcfg.get("endpoints") or {}).get("usage_summary") or {}).get("fields") or {}
            cost = get_path(us, f.get("total_cost_usd", "totals.total_cost_usd"))
            toks = get_path(us, f.get("total_tokens", "totals.total_tokens"))
            if cost and toks:
                vendor_eff = float(cost) / (float(toks) / 1e6)
                our_eff = (claims.get("provider_effective") or {}).get(prov)
                src = "provider_effective claim"
                if our_eff is None:
                    our_eff = (claims.get("provider_seed") or {}).get(prov)
                    src = "seed (_SEED_RATES)"
                if our_eff is not None and _pct(float(our_eff), vendor_eff) >= tolerance:
                    token_m = float(toks) / 1e6
                    findings.append({
                        "kind": "provider_effective", "provider": prov,
                        "model": "*", "component": "effective_usd_per_M",
                        "our": float(our_eff), "vendor": vendor_eff,
                        "ratio": (float(our_eff) / vendor_eff) if vendor_eff else None,
                        "delta_pct": _pct(float(our_eff), vendor_eff) * 100.0,
                        "claim_source": src,
                        "money_at_stake_usd": abs(vendor_eff - float(our_eff)) * token_m,
                        "window_tokens": float(toks),
                    })
    return findings


def render(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        ratio = f.get("ratio")
        inv = (1.0 / ratio) if ratio else float("inf")
        suffix = f" (vendor/our={inv:.1f}x)" if inv and inv > 1.05 else ""
        lines.append(
            f"[price-truth] {f['provider']}"
            f"{(':' + f['model']) if f.get('model') and f['model'] != '*' else ''} "
            f"{f['component']}: our={f['our']:.6f} vendor={f['vendor']:.6f} "
            f"ratio={ratio:.2f}x{suffix} delta={f['delta_pct']:.0f}% "
            f"(invariant |our-vendor|/vendor < {f.get('tolerance', DEFAULT_TOLERANCE):.0%})")
        if f.get("money_at_stake_usd") is not None:
            lines.append(
                f"    money at stake: ${f['money_at_stake_usd']:.2f} over "
                f"{f['window_tokens']/1e6:.1f} Mtok "
                f"(claim source: {f.get('claim_source','?')})")
    return "\n".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="F1 price belief-vs-truth audit")
    ap.add_argument("--config", default=None)
    ap.add_argument("--vendor-file", default=None)
    ap.add_argument("--claim-file", default=None)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD replay date")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    config_path = resolve_config(args.config)
    if config_path is None:
        print("[price-truth] cannot locate vendor_truth_sources.json "
              "(set --config or VENDOR_TRUTH_SOURCES)", file=sys.stderr)
        return 1
    cfg = load_config(config_path)
    tolerance = float(((cfg.get("thresholds") or {}).get("price_tolerance",
                                                           DEFAULT_TOLERANCE)))

    as_of_ts = None
    if args.as_of:
        import datetime
        try:
            as_of_ts = datetime.datetime.strptime(
                args.as_of, "%Y-%m-%d").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            print(f"[price-truth] bad --as-of {args.as_of!r} (want YYYY-MM-DD)",
                  file=sys.stderr)
            return 1

    # vendor payload precedence: --vendor-file > as-of snapshot > live
    vendor = None
    if args.vendor_file:
        vendor = json.loads(Path(args.vendor_file).expanduser().read_text())
    elif args.as_of:
        snap = (repo_root(config_path) / "state" / "fleet" / "truth_snapshots"
                / f"price-{args.as_of}.json")
        if snap.is_file():
            vendor = json.loads(snap.read_text())
    if vendor is None:
        try:
            vendor = fetch_vendor(cfg, load_env())
        except RuntimeError as exc:
            print(f"[price-truth] {exc}", file=sys.stderr)
            return 1

    claims = load_claims(cfg, config_path, args.claim_file, as_of_ts)
    findings = compute_findings(cfg, claims, vendor, tolerance)
    for f in findings:
        f["tolerance"] = tolerance

    if not findings:
        if args.verbose:
            print(f"[price-truth] OK — {len(cfg.get('providers', {}))} provider(s) "
                  f"within {tolerance:.0%} of vendor truth")
        return 0

    if args.json:
        print(json.dumps({"findings": findings}, indent=2, sort_keys=True))
    else:
        print(render(findings))
        print(f"[price-truth] {len(findings)} belief-vs-truth violation(s) "
              f"(source: {config_path.name})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
