#!/usr/bin/env python3
"""provider_probe.py — per-endpoint health probe feeding the Kalman router (D-136).

The flat market router prices lanes from the Kalman filters. It only routes
around a dead endpoint if that endpoint's health is *observed* and turned into a
price penalty (``price_kalman.health_pricing_factor``). 402s are marked unfunded
reactively, but timeouts / broken pipes / empty streams were not, so a lane
could sit dead while the router kept trying it.

This probe periodically checks every configured endpoint and writes:

  ~/.hermes/bot/provider_probe.json
    { "<provider>": {ts, ok, http, latency_ms, ttft_ms, error,
                      failure_streak, success_streak, healthy}, ... }

``flat_router`` reads this file in ``_is_provider_healthy`` / the price path so
an unhealthy endpoint is priced up (+inf past the streak threshold) and routed
around. Hysteresis: unhealthy after FAIL_TH consecutive failures, healthy again
after OK_TH consecutive successes.

Modes:
  provider_probe.py                 # cheap: GET /models (no token spend)
  provider_probe.py --deep          # + tiny chat completion (max_tokens=1) for TTFT
  provider_probe.py --json          # print the state
  provider_probe.py --providers a,b # subset

The z.ai keys (`ours`/`friend`) are CHEAP_ONLY — /models only, never the deep
completion: the probe must not spend the metered quota it exists to observe.

Exit: 0 always (probe result is data, not process failure).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
# D-140: write to the CANONICAL bot dir the router reads, NOT the profile
# HERMES_HOME. flat_router (in ~/.hermes/bot, under zai-proxy with HERMES_HOME
# unset) reads Path(flat_router).parent/provider_probe.json. When this script ran
# from the manager-profile cron it wrote ~/.hermes/profiles/manager/bot/... — a
# path the router never reads — so the probe health was silently ignored. This
# pins the output to the path the router actually uses (override with
# PROVIDER_PROBE_OUT for tests/non-standard layouts).
OUT = Path(os.environ.get(
    "PROVIDER_PROBE_OUT",
    os.path.expanduser("~/.hermes/bot/provider_probe.json")))
ENV_FILES = [HERMES / ".env", HERMES / "profiles" / "manager" / ".env",
             Path.home() / ".hermes" / ".env"]

FAIL_TH = 2   # consecutive failures -> unhealthy
OK_TH = 2     # consecutive successes -> healthy again
TIMEOUT = 12

# Lanes whose probe must stay CHEAP-ONLY (never the deep tiny completion).
# z.ai keys are quota-metered: the deep probe would spend real quota on every
# "--deep" tick just to observe a lane whose auth/reachability GET /models
# already proves (HTTP 200, zero token spend). Same evidence for the
# router_state re-admit gate, none of the burn.
CHEAP_ONLY = {"ours", "friend"}

# provider -> (base_url, [key env names in priority order], probe model)
#
# 2026-09-27 (pr/ds-v41-consolidation): the probe model MUST be a V4.1 DeepSeek
# Flash id, never the older V4 and never a retired tag. The ollama canaries used
# to be ``deepseek-v4-flash:0731``, which ollama RETIRED 2026-09-25 — verified
# live: POST /v1/chat/completions → HTTP 410 "was retired at 2026-09-25 00:00:00
# -0700 PDT". A deep tick (every ``--deep-every``, default 5) therefore failed
# on EVERY ollama lane (``rec["ok"] = 0 < deep_http < 400`` → False), so after
# FAIL_TH consecutive deep ticks the probe file marked all four ollama lanes
# unhealthy and the flat router priced them to +inf — silently removing the
# fleet's biggest free V4.1 capacity. The live replacement tag is
# ``deepseek-v4.1-flash`` (HTTP 429 weekly-limit ⇒ it exists).
#
# The other lanes' canaries used the older-V4 slugs too (neuralwatt
# ``deepseek-v4-flash``, openrouter ``deepseek/deepseek-v4-flash``, deepinfra
# ``deepseek-ai/DeepSeek-V4-Flash``, deepseek-direct ``deepseek-v4-flash``):
# the probe was spending real tokens on the older model it exists to observe
# the replacement for. They now use each lane's V4.1 native id.
#
# Lanes no longer in the DeepSeek-flash family (ppq, opencode_go) probe a model
# the router actually routes to them instead of the older V4.
PROVIDERS: dict[str, tuple[str, list[str], str]] = {
    "ollama_cloud":   ("https://ollama.com/v1", ["OLLAMA_CLOUD_API_KEY"], "deepseek-v4.1-flash"),
    "ollama_cloud_2": ("https://ollama.com/v1", ["OLLAMA_CLOUD_API_KEY_2"], "deepseek-v4.1-flash"),
    "ollama_cloud_3": ("https://ollama.com/v1", ["OLLAMA_CLOUD_API_KEY_3_STOIC_HERSCHEL_499"], "deepseek-v4.1-flash"),
    "ollama_cloud_4": ("https://ollama.com/v1", ["OLLAMA_CLOUD_API_KEY_4_SLEEPY_EASLEY_477"], "deepseek-v4.1-flash"),
    "neuralwatt":     ("https://api.neuralwatt.com/v1", ["NEURALWATT_API_KEY"], "deepseek-v4.1-flash"),
    # chutes' only DeepSeek slug IS the older V4 (opt-in id) — it has no V4.1
    # model, so its canary stays the -TEE slug (nothing routes there by default).
    "chutes":         ("https://llm.chutes.ai/v1", ["CHUTES_API_KEY"], "deepseek-ai/DeepSeek-V4-Flash-0731-TEE"),
    "openrouter":     ("https://openrouter.ai/api/v1", ["OPENROUTER_API_KEY"], "deepseek/deepseek-v4.1-flash"),
    "deepseek":       ("https://api.deepseek.com", ["DEEPSEEK_API_KEY"], "deepseek-flash"),
    "deepinfra":      ("https://api.deepinfra.com/v1/openai", ["DEEPINFRA_API_KEY"], "deepseek-ai/DeepSeek-V4.1-Flash"),
    # PPQ advertises no V4.1 flash slug; probe the GLM model we actually route here.
    "ppq":            ("https://api.ppq.ai/v1", ["PPQ_API_KEY"], "z-ai/glm-5.2"),
    "telnyx":         ("https://api.telnyx.com/v2/ai", ["TELNYX_API_KEY"], "moonshotai/Kimi-K3"),
    # opencode_go is out of the DeepSeek-flash family (legacy slug only); probe
    # the DeepSeek model the router still routes to it.
    "opencode_go":    ("https://opencode.ai/zen/v1", ["OPENCODE_GO_API_KEY"], "deepseek-v4-pro"),
    # z.ai keys (t_ec4ad19d). router_state.readmit_probe_healthy_backoff() only
    # considers a key that has a FRESH probe-healthy row in this file, so without
    # these entries a latched `ours` could recover only by backoff expiry — the
    # every-30s "re-admitted N probe-healthy latched key(s)" line never named a
    # z.ai lane. CHEAP-ONLY (see above): /models 200 is the evidence; the deep
    # completion would spend the quota the probe exists to observe.
    # `friend` reports no-key here because ZAI_API_KEY is no longer configured
    # (lane removed 2026-09-18); the row is kept so its absence is visible in the
    # probe snapshot rather than silently missing.
    "ours":           ("https://api.z.ai/api/coding/paas/v4", ["ZAI_OUR_KEY"], "glm-5.3"),
    "friend":         ("https://api.z.ai/api/coding/paas/v4", ["ZAI_API_KEY"], "glm-5.3"),
}


#: ADR-020: the in-memory OpenBao materialization of the providers secret,
#: written by ``openbao-env.service`` and loaded as a systemd EnvironmentFile by
#: zai-proxy.service (sd-notify ``%t`` = /run/user/<uid>).
OPENBAO_PROVIDERS_ENV = Path(
    os.environ.get(
        "OPENBAO_PROVIDERS_ENV",
        f"/run/user/{os.getuid()}/hermes-openbao/providers.env"))


def _source_openbao_env(env: dict) -> None:
    """Fill provider keys from the OpenBao-materialized env file (in-memory).

    The probe hands its child environment to no one, but it *reads* :data:`ENV`
    at import. When the probe runs from a bare cron shell (``env`` whose
    DEEPSEEK_API_KEY was deleted from every ``.env`` at the ADR-020 cutover) it
    sees NO key and records ``error="no-key"`` -> ``healthy=False`` -> the flat
    router's ``_probe_says_unhealthy`` prices a FUNDED, SERVING lane (deepseek
    direct) to +inf and skips it as undeliverable. Measured 2026-10-03:
    ``deepseek`` sat at ``failure_streak 425`` with ``error "no-key"`` while a
    direct completion through the router returned 200 ``X-Failover-Provider:
    deepseek`` — the exact false-HOLD source for t_7b2f04d9.

    Order mirrors zai_proxy._load_external_keys: files are also read (rather
    than only exported env) because the probe is frequently invoked from a shell
    that never sourced the file. Fail-open: a missing/unreadable file adds
    nothing, and the callers' existing ``"x" not in env`` guards mean a real
    ``os.environ`` value always wins.
    """
    try:
        if OPENBAO_PROVIDERS_ENV.is_file():
            for line in OPENBAO_PROVIDERS_ENV.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#", 1)[0].strip().strip("'").strip('"')
                if v and not env.get(k.strip()):
                    env[k.strip()] = v
    except OSError:
        pass


def _load_env() -> dict:
    env = dict(os.environ)
    # ADR-020: prefer the OpenBao providers secret (in-memory); .env is
    # the fallback below.
    try:
        import sys as _sys
        _sys.path.insert(0, str(HERMES / "scripts"))
        import fleet_secret as _fs  # noqa
        for _k, _v in _fs.get_secret("providers").items():
            if str(_v):
                env[_k] = str(_v)
    except Exception:
        pass
    # Secrets can also materialize as the systemd EnvironmentFile the proxy
    # consumes; source it directly (file read) so a bare cron shell still sees
    # the provider keys. Runs BEFORE the .env fallback so a stale .env cannot
    # shadow OpenBao.
    _source_openbao_env(env)
    for f in ENV_FILES:
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#", 1)[0].strip().strip("'").strip('"')
                env.setdefault(k.strip(), v)
        except OSError:
            continue
    return env


ENV = _load_env()
CTX = ssl.create_default_context()


def _req(url: str, key: str, method: str = "GET", body: dict | None = None,
         timeout: int = TIMEOUT) -> tuple[int, float, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            r.read(1024)
            return r.status, (time.time() - t0) * 1000.0, ""
    except urllib.error.HTTPError as e:
        return e.code, (time.time() - t0) * 1000.0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return -1, (time.time() - t0) * 1000.0, type(e).__name__


def _key(env: dict, names: list[str]) -> str:
    for n in names:
        if env.get(n):
            return env[n]
    return ""


def probe_one(name: str, deep: bool) -> dict:
    base, keynames, model = PROVIDERS[name]
    key = _key(ENV, keynames)
    rec: dict = {"provider": name, "ts": int(time.time()), "model": model}
    if not key:
        rec.update(ok=False, http=None, error="no-key", latency_ms=None, ttft_ms=None)
        return rec
    # 1) cheap reachability/auth check
    http, lat, err = _req(f"{base}/models", key)
    if http == -1 and "HTTP" not in err:
        http2, lat2, err2 = _req(f"{base}/v1/models", key)  # some bases are pre-/v1
        if http2 != -1:
            http, lat, err = http2, lat2, err2
    # 2) a 404/405 on /models still means reachable+authed-ish; treat <400 or 404 as reachable
    reachable = (0 < http < 400) or http == 404
    rec.update(http=http, latency_ms=round(lat, 1), error="" if reachable else err,
               ok=reachable)
    if deep and reachable and name not in CHEAP_ONLY:
        b = {"model": model, "messages": [{"role": "user", "content": "hi"}],
             "max_tokens": 1, "stream": False}
        th, tlat, terr = _req(f"{base}/chat/completions", key, "POST", b)
        rec["ttft_ms"] = round(tlat, 1)
        rec["deep_http"] = th
        rec["ok"] = 0 < th < 400
        if th == -1 or th >= 400:
            rec["error"] = terr or f"deep HTTP {th}"
    return rec


def _merge_hysteresis(prev: dict, rec: dict) -> dict:
    old = prev.get(rec["provider"]) or {}
    deep = "deep_http" in rec
    # GENERATION latch (t_0b25ccf3 D2). A lane whose real dials return 402
    # (neuralwatt / ppq / chutes / openrouter) still answers 200 on the cheap
    # ``/models`` probe, and the cheap ticks used to RESET ``failure_streak`` —
    # so FAIL_TH was never crossed, ``healthy`` stayed True, and
    # ``router_state.periodic_readmit`` re-admitted a dead lane on every
    # cool-down. Track the REAL generation result separately, carry it across
    # cheap ticks, and let it gate ``healthy`` and the re-admit evidence set
    # (``gen_ok`` is what probe_healthy_names(require_generation=True) reads).
    if deep:
        th = rec.get("deep_http")
        if rec.get("ok"):
            rec["gen_ok"] = True
            rec["gen_fail_streak"] = 0
            rec["gen_dead"] = False
        else:
            rec["gen_ok"] = False
            rec["gen_fail_streak"] = int(old.get("gen_fail_streak") or 0) + 1
            # A real generation returning 402/403 is DEFINITIVE (no funding /
            # auth) -> bench the lane immediately; the cheap /models 200 must
            # not heal it. Any OTHER deep failure (timeout, 5xx, a retired
            # probe model's 410) is ambiguous -> the ordinary FAIL_TH streak
            # decides, so a stale probe *model* can never silently remove a
            # lane that still serves its other models (the ollama regression
            # this file's history warns about).
            rec["gen_dead"] = (th in (402, 403)
                               or rec["gen_fail_streak"] >= FAIL_TH)
    elif old:
        for _f in ("gen_ok", "gen_fail_streak", "gen_dead"):
            if _f in old:
                rec[_f] = old.get(_f)
    gen_dead = bool(rec.get("gen_dead"))

    if not old:
        # Cold start: the first observation is authoritative.
        rec["failure_streak"] = 0 if rec.get("ok") else 1
        rec["success_streak"] = 1 if rec.get("ok") else 0
        rec["healthy"] = (bool(rec.get("ok")) if rec.get("ok")
                          else FAIL_TH <= 1) and not gen_dead
        return rec
    if rec.get("ok"):
        ss = int(old.get("success_streak", 0)) + 1
        rec["success_streak"] = ss
        rec["failure_streak"] = 0
        rec["healthy"] = (ss >= OK_TH or bool(old.get("healthy"))) and not gen_dead
    else:
        fs = int(old.get("failure_streak", 0)) + 1
        rec["failure_streak"] = fs
        rec["success_streak"] = 0
        rec["healthy"] = (False if fs >= FAIL_TH
                          else bool(old.get("healthy", True))) and not gen_dead
    return rec


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--deep-every", type=int, default=5,
                    help="run the tiny-completion probe every Nth cheap tick")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--providers", default="")
    args = ap.parse_args(argv)

    names = [n for n in (args.providers.split(",") if args.providers else PROVIDERS) if n in PROVIDERS]
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        prev = {}

    # Deep cadence: explicit --deep always deep; otherwise every Nth cheap tick.
    tick = int((prev.get("_meta") or {}).get("tick", 0)) + 1
    deep = args.deep or (args.deep_every > 0 and tick % args.deep_every == 0)

    state = dict(prev)
    bad = 0
    for n in names:
        rec = _merge_hysteresis(prev, probe_one(n, deep))
        state[n] = rec
        flag = "ok " if rec.get("healthy") else "BAD"
        if not rec.get("healthy"):
            bad += 1
        print(f"[probe] {flag} {n:14} http={rec.get('http')} "
              f"lat={rec.get('latency_ms')}ms ttft={rec.get('ttft_ms')} "
              f"fail={rec.get('failure_streak')} {rec.get('error') or ''}")

    state["_meta"] = {"ts": int(time.time()), "deep": deep, "tick": tick,
                      "unhealthy": [k for k, v in state.items()
                                    if isinstance(v, dict) and v.get("healthy") is False]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(OUT)
    if args.json:
        print(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
