#!/usr/bin/env python3
"""reviewer_assign.py — pick the CHEAPEST live reviewer that is NOT the author's family.

Policy (operator, 2026-09-17): reviews go to the cheapest review LLM whose family
differs from the worker that produced the work, and which has a live lane. This
replaces the old hard-pin to `worker-reviewer-kimi` (kimi-k3), which drained the
scarce neuralwatt lane and 503'd whenever kimi-k3's lanes were empty.

Operator policy (2026-09-22), internal kanban review path: the *creator* of a
review card must CHOOSE the reviewer family with this module — glm by default,
the author's family excluded, kimi only when no cheaper cross-family lane is
live — and pin gate 2 to a different cross-family (deepseek) where applicable.
`choose_markers()` returns exactly the `reviewer:<family>` / `gate2:<family>`
tokens such a card carries; `fleet_queue.reviewer_profile` maps the reviewer
token to `worker-reviewer-<family>` on whichever node claims the card.

Pure selection (`choose`, `choose_markers`) is separated from the live probe for
testability.

Usage:
  reviewer_assign.py --author-profile worker-heavy [--live p1,p2] [--json]
  reviewer_assign.py --markers [--author-family deepseek] [--live p1,p2] --json
  # prints the chosen profile name, or "skip"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
_HOME = Path(os.path.expanduser("~/.hermes"))
_SCRIPTS = Path(__file__).resolve().parent


def _first_existing(*cands: Path) -> Path | None:
    for c in cands:
        try:
            if Path(c).is_file():
                return Path(c)
        except OSError:
            continue
    return None


def _asset(name: str) -> Path:
    """Deployed-or-repo location of a fleet review asset (map / cost policy).

    Resolution is layout-sensitive, and getting it wrong is silent: cron sets
    HERMES_HOME to a *profile* dir (`~/.hermes/profiles/manager`), so the old
    `HERMES/bot/<name>`-else-repo-relative lookup resolved
    `~/.hermes/profiles/state/fleet/...` and raised FileNotFoundError, which the
    review-card creator's chooser wrapper swallows — reviewer choice never
    engaged in production while every repo test passed (2026-09-23, t_7e34bae1).

    Try, in order: the HERMES bot dir (role install target), plain
    `~/.hermes/bot`, the module's own dir, then every ancestor's `state/fleet`
    (the repo/worktree layout).
    """
    cands = [HERMES / "bot" / name, _HOME / "bot" / name, _SCRIPTS / name]
    for base in (_SCRIPTS, *_SCRIPTS.parents[:6]):
        cands.append(base / "state" / "fleet" / name)
    return _first_existing(*cands) or (_SCRIPTS.parents[1] / "state" / "fleet" / name)


DEFAULT_MAP = _asset("review_family_priority.json")
DEFAULT_POLICY = _asset("review_cost_policy.json")

#: IPv4, explicitly. `localhost` resolves to `::1` first on this fleet, which
#: hits an SSH reverse tunnel rather than the proxy — every curl in the runbooks
#: says `127.0.0.1` for exactly this reason.
PROXY_DEFAULT = "http://127.0.0.1:9099"
PROXY = os.environ.get("ZAI_PROXY_URL", PROXY_DEFAULT)

#: The fleet_queue.reviewer_profile / reviewer-profile naming convention.
REVIEWER_PREFIX = "worker-reviewer-"

# Fallback family detection from a model id (used to map the author's profile).
_FAMILY_HINTS = {
    "glm": "zhipu", "zhipu": "zhipu", "kimi": "moonshot", "moonshot": "moonshot",
    "qwen": "alibaba", "alibaba": "alibaba", "deepseek": "deepseek",
    "gpt": "openai", "claude": "anthropic", "gemini": "google",
}


def family_of_model(model: str) -> str:
    m = (model or "").lower()
    for k, v in _FAMILY_HINTS.items():
        if k in m:
            return v
    return "unknown"


#: Capability ordinals (from each profile's benchmark class). A 'risky' review
#: requires capability >= pro, so a simple/cheap model can't take a risky review.
_CAP_RANK = {"solid": 1, "pro": 2}


def choose(profiles: list[dict], author_fam: str, live: set[str],
           review_class: str = "simple", policy: dict | None = None) -> str:
    """Cheapest live profile whose family != author_fam and capability meets the
    review class floor. Returns "" if none.

    simple (default): cheapest floor-meeting model (e.g. kimi-k2.7-code over
    kimi-k3). risky: capability >= pro (a stronger model is worth the cost).
    """
    classes = (policy or {}).get("classes", {})
    min_rank = _CAP_RANK.get(classes.get(review_class, {}).get("min_capability", "solid"), 1)
    cands = [
        p for p in profiles
        if p.get("family") != author_fam
        and p.get("family") != "unknown"
        and p.get("profile") in live
        and _CAP_RANK.get(p.get("capability", "solid"), 1) >= min_rank
    ]
    if not cands:
        return ""
    cands.sort(key=lambda p: (p.get("cost_rank", 999), p.get("profile", "")))
    return cands[0]["profile"]


def suffix(profile: str) -> str:
    """`worker-reviewer-glm` -> `glm` — the token a `reviewer:` marker carries.

    `fleet_queue.reviewer_profile` reverses this mapping
    (`reviewer:glm` -> `worker-reviewer-glm`), so a marker is only meaningful for
    profiles that follow the convention; anything else has no suffix.
    """
    p = (profile or "").strip()
    return p[len(REVIEWER_PREFIX):] if p.startswith(REVIEWER_PREFIX) else ""


def family_of_profile(profiles: list[dict], profile: str) -> str:
    for p in profiles:
        if p.get("profile") == profile:
            return str(p.get("family") or "unknown")
    return "unknown"


def choose_markers(profiles: list[dict], author_fam: str, live: set[str],
                   review_class: str = "simple",
                   policy: dict | None = None) -> dict[str, str]:
    """The `reviewer`/`gate2` marker families for one internal review card.

    gate 1 (`reviewer`) is the cheapest live cross-family model (`choose`).
    gate 2 (`gate2`) is the cheapest live model whose family differs from BOTH
    the author and gate 1 — a *second*, independent cross-family audit. Either
    value is "" when no such lane is live (the card creator then falls back to
    its static default rather than inventing a reviewer).
    """
    rev = choose(profiles, author_fam, live, review_class, policy)
    if not rev:
        return {"reviewer": "", "gate2": ""}
    rev_fam = family_of_profile(profiles, rev)
    others = [p for p in profiles if str(p.get("family") or "") != rev_fam]
    gate2 = choose(others, author_fam, live, review_class, policy)
    return {"reviewer": suffix(rev), "gate2": suffix(gate2)}


def marker_text(markers: dict | None) -> str:
    """`reviewer:glm gate2:ds` — the tokens a review card carries for the fleet.

    Empty parts are dropped, so a single-lane day yields `reviewer:glm` only.
    """
    parts = []
    for key in ("reviewer", "gate2"):
        value = (markers or {}).get(key) or ""
        if value:
            parts.append(f"{key}:{value}")
    return " ".join(parts)


def author_family(profile: str) -> str:
    """Family of the model an author profile is pinned to ("" profile -> unknown)."""
    if not profile:
        return "unknown"
    pin = ""
    try:
        cfg = (HERMES / "profiles" / profile / "config.yaml").read_text()
        for line in cfg.splitlines():
            if line.strip().startswith("default:"):
                pin = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    return family_of_model(pin) or "unknown"


# ── liveness: PROBE the model, do not infer it from the catalog ──────────────
#
# Measured 2026-09-28 (tollgate-module-basic-go): the fleet kept reporting
# "cross-family review is structurally unavailable" and asking the operator for
# waivers, excusing the mandatory D-128 §4 gate. Direct probes at 127.0.0.1:9099
# showed every lane the witnesses called dead was ALIVE — glm-5.2/glm-5.3 ->
# 200, tencent/hy4-preview -> 200 (SiliconFlow), kimi-k3/kimi-k2.7-code -> 200.
# Only qwen3.5:397b was genuinely dead, and its 503 carried an EMPTY candidate
# list (`candidates_considered: []`), i.e. no provider declares that model.
#
# The old predicate had two independent bugs:
#   (a) it required the model to be ADVERTISED in /v1/models, but the router
#       serves ids it does not list — so a healthy lane looked missing;
#   (b) it returned the EMPTY SET whenever dispatch_gate dipped, so ONE bad
#       capacity reading became the verdict "no cross-family reviewer exists".
#
# Both are the same failure mode: a momentary observation promoted to a
# structural conclusion. Hence: probe, bound the probe, and never veto on gate.

#: Six reviewer models are probed per review-card creation, so every dimension is
#: bounded — concurrency, per-request timeout, retries, and a hard wall-clock.
PROBE_TIMEOUT = 6.0
PROBE_RETRIES = 2
PROBE_BACKOFF = 1.5
PROBE_BUDGET = 15.0
PROBE_CACHE_TTL = 60.0
_PROBE_CACHE = Path(tempfile.gettempdir()) / "reviewer_assign_live.json"


def _advertised_models(**_) -> set[str]:
    """Ids the router ADVERTISES in /v1/models.

    Advertisement is NOT liveness (see the block comment above). This exists only
    as the `--no-probe` fallback and as cache context.
    """
    try:
        with urllib.request.urlopen(f"{PROXY}/v1/models", timeout=8) as r:
            return {m.get("id") for m in json.loads(r.read()).get("data", [])}
    except Exception:
        return set()


def _dispatch_gate_can_dispatch(**_) -> bool:
    """Advisory capacity reading. NEVER a veto — see `_live_profiles`."""
    try:
        with urllib.request.urlopen(
                f"{PROXY}/v1/dispatch_gate?estimated_tokens=200000&task_type=coding",
                timeout=8) as r:
            return bool(json.loads(r.read()).get("can_dispatch", True))
    except Exception:
        return True


def _http_post_json(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """POST JSON -> (status, body). Never raises on an HTTP error.

    Separated out so `_probe_model` can be driven by a scripted opener in tests.
    """
    import urllib.error

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _probe_model(model: str, *, opener=None, retries: int = PROBE_RETRIES,
                 timeout: float = PROBE_TIMEOUT, sleep=time.sleep) -> bool:
    """Can the router serve `model` RIGHT NOW? True iff it answers 2xx.

    Two DIFFERENT 503 shapes, and conflating them is the bug this fixes:
      * `candidates_considered == []` -> NO LANE DECLARES THIS MODEL. That is a
        config fact, not load, so it is deterministic and NOT retried.
      * a non-empty candidate list -> the lanes were dialled and were busy. That
        is a BURST, not an outage (glm-5.3 measured exactly this shape and answered
        200 minutes later), so it IS retried with a short backoff.
    """
    post = opener or _http_post_json
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "ok"}]}
    attempts = max(0, retries) + 1
    for attempt in range(attempts):
        status, body = post(f"{PROXY}/v1/chat/completions", payload, timeout)
        if 200 <= int(status or 0) < 300:
            return True
        if status == 503 and not body.get("candidates_considered"):
            return False            # structural no-lane; retrying cannot help
        if status and status != 503:
            return False            # a definitive non-503 rejection
        if attempt < attempts - 1:
            try:
                sleep(PROBE_BACKOFF * (attempt + 1))
            except Exception:
                pass
    return False


def _probe_profiles(profiles: list[dict], probe=None,
                    budget: float = PROBE_BUDGET) -> set[str]:
    """Concurrently probe every profile's model, bounded by `budget` seconds."""
    fn = probe or _probe_model
    wanted: list[tuple[str, str]] = [
        (str(p.get("profile")), str(p.get("model")))
        for p in profiles if p.get("model") and p.get("profile")]
    if not wanted:
        return set()
    live: set[str] = set()
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        with ThreadPoolExecutor(max_workers=min(6, len(wanted))) as ex:
            futs = {ex.submit(fn, model): prof for prof, model in wanted}
            done, _ = wait(list(futs), timeout=budget)
            for fut in done:
                try:
                    if fut.result():
                        live.add(futs[fut])
                except Exception:
                    pass
    except Exception:
        pass                        # probing must never break selection
    return live


def _cache_key(profiles: list[dict]) -> str:
    return "|".join(f"{p.get('profile')}:{p.get('model')}" for p in profiles)


def _cache_read(profiles: list[dict]) -> set[str] | None:
    try:
        data = json.loads(_PROBE_CACHE.read_text())
        if (data.get("key") == _cache_key(profiles)
                and time.time() - float(data.get("ts", 0)) < PROBE_CACHE_TTL):
            return set(data.get("live", []))
    except Exception:
        pass
    return None


def _cache_write(profiles: list[dict], live: set[str]) -> None:
    try:
        _PROBE_CACHE.write_text(json.dumps(
            {"key": _cache_key(profiles), "ts": time.time(), "live": sorted(live)}))
    except Exception:
        pass


def _live_profiles(profiles: list[dict], *, probe=None, no_probe: bool = False,
                   use_cache: bool = True) -> set[str]:
    """Profiles the router can actually serve right now.

    Liveness is PROBED per model. Two things must never happen here, both of which
    previously produced a false "structurally unavailable" verdict:
      * treating "not advertised in /v1/models" as dead;
      * letting a single `dispatch_gate` dip collapse the pool to EMPTY.
    """
    if no_probe:
        advertised = _advertised_models()
        return {p["profile"] for p in profiles
                if p.get("model") and (p["model"] in advertised
                                       or any(p["model"].split("/")[-1] in x
                                              for x in advertised))}
    if use_cache:
        cached = _cache_read(profiles)
        if cached is not None:
            return cached
    live = _probe_profiles(profiles, probe=probe)
    if use_cache:
        _cache_write(profiles, live)
    if live and not _dispatch_gate_can_dispatch():
        # Advisory ONLY. A capacity dip says nothing about which reviewer families
        # exist; reporting it as "no reviewer" is how the false waivers were born.
        print("reviewer_assign: dispatch_gate reports no dispatchable capacity; "
              "keeping probed live lanes (advisory only)", file=sys.stderr)
    return live


def load_config(map_path: str | None = None, policy_path: str | None = None
                ) -> tuple[list[dict], dict]:
    """(profiles, policy) from the deployed-or-repo default locations."""
    data = json.loads(Path(map_path or DEFAULT_MAP).read_text())
    profiles = data.get("profiles", []) if isinstance(data, dict) else data
    policy: dict = {}
    try:
        policy = json.loads(Path(policy_path or DEFAULT_POLICY).read_text())
    except Exception:
        pass
    return profiles, policy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--author-profile", default="")
    ap.add_argument("--author-family", default="")
    ap.add_argument("--live", default="")
    ap.add_argument("--map", default=str(DEFAULT_MAP))
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--review-class", default="simple", choices=["simple", "risky"])
    ap.add_argument("--no-probe", action="store_true",
                    help="offline/air-gapped: fall back to /v1/models advertisement "
                         "instead of probing each reviewer model (less accurate)")
    ap.add_argument("--markers", action="store_true",
                    help="emit reviewer:/gate2: marker families (internal review cards)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    profiles, policy = load_config(args.map, args.policy)

    author_fam = args.author_family or author_family(args.author_profile)
    author_fam = author_fam or "unknown"

    live = (set(p.strip() for p in args.live.split(",") if p.strip())
            or _live_profiles(profiles, no_probe=args.no_probe))

    if args.markers:
        markers = choose_markers(profiles, author_fam, live, args.review_class, policy)
        out = {
            "author_family": author_fam,
            "review_class": args.review_class,
            "live": sorted(live),
            "reviewer": f"{REVIEWER_PREFIX}{markers['reviewer']}" if markers["reviewer"] else "skip",
            "gate2": f"{REVIEWER_PREFIX}{markers['gate2']}" if markers["gate2"] else "skip",
            "marker": marker_text(markers),
        }
        print(json.dumps(out) if args.json else (out["marker"] or "skip"))
        return 0

    picked = choose(profiles, author_fam, live, args.review_class, policy)
    out = {"author_family": author_fam, "review_class": args.review_class,
           "live": sorted(live), "reviewer": picked or "skip"}
    if args.json:
        print(json.dumps(out))
    else:
        print(picked or "skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
