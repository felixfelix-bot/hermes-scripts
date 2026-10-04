#!/usr/bin/env python3
"""decisions_channel_watchdog.py — the alert path for the operator-decisions push.

WHY THIS EXISTS (task t_a328b9a0)
--------------------------------
`decisions_digest.py` runs as a `no_agent` cron with `deliver=local`. Its only
failure surface was a line in a local log file, so a decision channel that
delivered nothing looked exactly like one that delivered everything: 110
consecutive failing ticks, no alert, no operator, no board signal. This watchdog
is the missing delivery path.

CONTRACT
--------
* prints NOTHING when the channel is healthy → the `no_agent` cron stays silent;
* prints ONE loud alert block when degraded, then stays quiet for
  `WATCHDOG_REPEAT_S` (default 6h) per distinct problem set, so a persistent
  outage does not spam every tick;
* prints a one-line "recovered" notice when a previously-alerting state clears;
* exit is ALWAYS 0 — a non-zero exit would itself be delivered as a failure, and
  a watchdog must not be the thing that looks broken.

WHAT IT CHECKS (independent of the digest: it re-probes the relay itself)
------------------------------------------------------------------------
1. health file present and fresh (`last_attempt` within `WATCHDOG_STALE_S`) —
   a digest that stopped running is the silent case this whole card is about;
2. `role=unknown` — the node cannot even determine whether it may post;
3. `role=writer` with `consecutive_failures > 0` — a real post failure, with the
   underlying error echoed;
4. `role=observer` while a `decisions_digest` cron job is still enabled on this
   node — a non-member running the writer job is the misconfiguration that
   produced the bogus 110-failure "outage";
5. live relay probe (`nak req` with this node's key) — a genuine outage.

Usage:
  decisions_channel_watchdog.py [--json]

Env seams (production defaults; used by tests):
  WATCHDOG_HEALTH_FILE, WATCHDOG_STATE_FILE, WATCHDOG_CRON_JOBS,
  WATCHDOG_STALE_S, WATCHDOG_REPEAT_S, WATCHDOG_PROBE, DECISIONS_NSEC_FILE,
  DECISIONS_OPS_CFG, WATCHDOG_CHANNEL_CFG
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
NAME = "operator-decisions"


def _path(env: str, default: str) -> Path:
    return Path(os.path.expanduser(os.environ.get(env) or default))


HEALTH = _path("WATCHDOG_HEALTH_FILE", str(HOME / ".hermes/state/decisions_post_health.json"))
STATE = _path("WATCHDOG_STATE_FILE", str(HOME / ".hermes/state/decisions_channel_watchdog.json"))
CHANNEL_CFG = _path("WATCHDOG_CHANNEL_CFG", str(HOME / ".hermes/bot/decisions_channel.json"))
OPS_CFG = _path("DECISIONS_OPS_CFG", str(HOME / ".hermes/bot/hermes_ops.json"))
CRON_JOBS = _path("WATCHDOG_CRON_JOBS", str(HOME / ".hermes/cron/jobs.json"))
NSEC_OVERRIDE = os.environ.get("DECISIONS_NSEC_FILE") or None
LEGACY_NSEC = "~/.hermes/keys/hermes-ops/cobrador.nsec"

STALE_S = int(os.environ.get("WATCHDOG_STALE_S", "7200"))
REPEAT_S = int(os.environ.get("WATCHDOG_REPEAT_S", "21600"))
PROBE = os.environ.get("WATCHDOG_PROBE", "1") != "0"
PROBE_TIMEOUT = int(os.environ.get("WATCHDOG_PROBE_TIMEOUT", "30"))

NAK = next((c for c in [os.path.expanduser("~/.local/bin/nak"),
                        "/usr/local/bin/nak", "/usr/bin/nak"]
            if Path(c).exists()), "nak")


def now_epoch() -> int:
    return int(time.time())


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)
    except OSError:
        pass


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def nsec_path() -> Path:
    if NSEC_OVERRIDE:
        return Path(os.path.expanduser(NSEC_OVERRIDE))
    try:
        p = json.loads(OPS_CFG.read_text()).get("node_nsec")
    except Exception:
        p = None
    return Path(os.path.expanduser(p or LEGACY_NSEC))


def relay() -> str:
    return _load(CHANNEL_CFG).get("relay", "wss://relay.orangesync.tech")


def group() -> str:
    return _load(CHANNEL_CFG).get("orange_group", "")


def _sec() -> str:
    try:
        return nsec_path().read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def digest_cron_enabled() -> bool:
    """True if a decisions_digest job is enabled in this node's cron store."""
    try:
        raw = json.loads(CRON_JOBS.read_text())
    except Exception:
        return False
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    seq = jobs.values() if isinstance(jobs, dict) else (jobs or [])
    for j in seq:
        if not isinstance(j, dict):
            continue
        if "decisions_digest" in str(j.get("script", "")):
            return j.get("enabled", True) is not False
    return False


def probe_relay() -> tuple[str, str]:
    """(state, detail) — ok | down | unknown. Independent of the digest."""
    if not PROBE:
        return "ok", "probe disabled"
    sec = _sec()
    if not sec:
        return "unknown", f"no node key ({nsec_path()})"
    g = group()
    if not g:
        return "unknown", "no channel config"
    args = [NAK, "req", "-k", "9", "-t", f"h={g}", "-l", "1", "--auth",
            "--sec", sec, relay()]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
    except Exception as e:
        return "unknown", f"probe error: {type(e).__name__}"
    both = ((r.stdout or "") + (r.stderr or "")).lower()
    for marker in ("failed", "refused", "unreachable", "closed: auth-required",
                   "connection took too long", "timed out"):
        if marker in both:
            return "down", both.strip()[-160:]
    return "ok", "authenticated read ok"


def collect_problems() -> list[str]:
    problems: list[str] = []
    now = now_epoch()
    health = _load(HEALTH)
    if not health:
        problems.append(f"no health file at {HEALTH} — the digest has never "
                        f"reported, so nothing can be verified")
    else:
        role = str(health.get("role", "writer"))
        last_attempt = health.get("last_attempt") or health.get("last_success")
        if not last_attempt:
            problems.append("health file has no last_attempt — digest state "
                            "is unreadable")
        elif now - int(last_attempt) > STALE_S:
            problems.append(
                f"digest not running: last attempt {_iso(last_attempt)} "
                f"({(now - int(last_attempt)) // 60} min ago, stale > "
                f"{STALE_S // 60} min)")
        if role == "unknown":
            problems.append(
                "channel role unknown — this node cannot determine whether it "
                f"may post: {str(health.get('last_error'))[:160] or 'no detail'}")
        elif role == "writer":
            n = int(health.get("consecutive_failures", 0) or 0)
            if n > 0:
                problems.append(
                    f"writer node failed to post on {n} consecutive tick(s): "
                    f"{str(health.get('last_error'))[:200] or 'no detail'}")
        elif role == "observer":
            if digest_cron_enabled():
                problems.append(
                    "duplicate decisions writer job enabled on a non-member "
                    "(observer) node — this node's pubkey is not in "
                    f"{NAME}'s member list, so the cron can never deliver and "
                    "only reports bogus failures (disable the job or make the "
                    "node a member)")
    if not any(p.startswith("duplicate") for p in problems):
        state, detail = probe_relay()
        if state == "down":
            problems.append(f"relay unreachable: {relay()} — {detail[:200]}")
    return problems


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
def render(problems: list[str]) -> str:
    lines = [f"ALERT decision-channel: {NAME} push surface is degraded "
             f"({len(problems)} problem(s))"]
    lines += [f"  - {p}" for p in problems]
    lines.append(f"  relay: {relay()}")
    lines.append(f"  health: {HEALTH}")
    lines.append("  Note: the digest cron is no_agent/deliver=local, so this "
                 "watchdog is the ONLY surface that reports a dead decision "
                 "channel to the operator.")
    return "\n".join(lines)


def main() -> int:
    now = now_epoch()
    problems = collect_problems()
    st = _load(STATE)
    key = " | ".join(problems)

    if not problems:
        if st.get("last_key"):
            print(f"OK decision-channel: {NAME} recovered — no problems remain "
                  f"(was: {str(st.get('last_key'))[:120]})")
            _save(STATE, {"last_key": "", "last_alert_at": 0,
                          "last_recovered_at": now})
        if "--json" in sys.argv:
            print(json.dumps({"problems": [], "role": _load(HEALTH).get("role")}))
        return 0

    if st.get("last_key") == key and now - int(st.get("last_alert_at", 0) or 0) < REPEAT_S:
        return 0   # same problem set, inside the repeat window: stay silent

    print(render(problems))
    _save(STATE, {"last_key": key, "last_alert_at": now})
    return 0


if __name__ == "__main__":
    sys.exit(main())
