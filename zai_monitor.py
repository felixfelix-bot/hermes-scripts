#!/usr/bin/env python3
"""Hermes watchdog: z.ai rate-limit monitor (responsibility #4, top priority).

Watchdog pattern: --no-agent cron script. stdout delivered verbatim; EMPTY = silent.
- Polls the 5-hour session quota + token quota from z.ai.
- Writes ~/.hermes/bot/zai_state.json every run (for the scheduler/other jobs).
- Alerts (stdout) ONLY when utilization crosses thresholds, suggesting a pause.

Other scheduled jobs should consult zai_state.json["throttle"] before running,
and skip themselves when True — this is the actual scheduler-throttle mechanism.

Schedule suggestion:
  hermes cron create --no-agent --script zai_monitor.py --name zai-watch --deliver local '15m'
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
STATE_PATH = Path.home() / ".hermes" / "bot" / "zai_state.json"

# thresholds (% of quota used)
WARN_PCT = int(os.environ.get("ZAI_WARN_PCT", "80"))     # soft: suggest pause
CRIT_PCT = int(os.environ.get("ZAI_CRIT_PCT", "92"))     # hard: must pause


def _extract_key(text: str) -> str | None:
    """Find ZAI_API_KEY/GLM_API_KEY in dotenv-style text. Handles export,
    quotes, inline comments, and leading whitespace. Ignores commented lines."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        for name in ("ZAI_API_KEY", "GLM_API_KEY"):
            if line.startswith(name + "="):
                val = line.split("=", 1)[1].strip()
                # strip matching surrounding quotes
                if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                    val = val[1:-1]
                # drop inline comment after a space (only if value unquoted)
                val = val.split(" #", 1)[0].strip()
                if val:
                    return val
    return None


def load_key() -> str | None:
    key = os.environ.get("ZAI_API_KEY") or os.environ.get("GLM_API_KEY")
    if key:
        return key
    for env_file in (Path.home() / ".hermes" / ".env", Path.home() / ".bashrc"):
        if env_file.is_file():
            found = _extract_key(env_file.read_text(errors="ignore"))
            if found:
                return found
    return None


def fetch_quota(key: str) -> dict | None:
    req = urllib.request.Request(QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def main() -> int:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = load_key()
    if not key:
        # no key -> can't monitor; stay silent so it doesn't spam, but record state
        STATE_PATH.write_text(json.dumps({"ts": int(time.time()), "ok": False, "error": "no_api_key", "throttle": False}, indent=2))
        return 0

    data = fetch_quota(key)
    if isinstance(data, dict) and data.get("_error"):
        STATE_PATH.write_text(json.dumps({"ts": int(time.time()), "ok": False, "error": data["_error"], "throttle": False}, indent=2))
        return 0  # transient network/API error: silent, don't spam

    limits = (data.get("data") or {}).get("limits") or []
    session_pct = token_pct = 0
    session_reset = token_reset = None
    session_detail = token_detail = None
    for lim in limits:
        t = lim.get("type")
        pct = int(lim.get("percentage") or 0)
        if t == "TIME_LIMIT":
            session_pct = pct
            session_reset = lim.get("nextResetTime")
            session_detail = lim
        elif t == "TOKENS_LIMIT":
            token_pct = max(token_pct, pct)  # max across all token-quota windows
            token_reset = lim.get("nextResetTime")

    peak = max(session_pct, token_pct)
    throttle = peak >= WARN_PCT
    critical = peak >= CRIT_PCT
    quota_pause = token_pct >= 85  # D-062: pause z.ai dispatch when token quota >= 85%

    # Friend's fallback key monitoring (D-066/D-067): ease off at 40%
    FRIEND_KEY = os.environ.get("ZAI_FALLBACK_API_KEY", "${ZAI_API_KEY:-***ZAI_FRIEND_ROTATED***}")
    friend_data = fetch_quota(FRIEND_KEY)
    friend_pct = 0
    friend_pause = False
    if isinstance(friend_data, dict) and not friend_data.get("_error"):
        for flim in ((friend_data.get("data") or {}).get("limits") or []):
            if flim.get("type") == "TOKENS_LIMIT":
                friend_pct = max(friend_pct, int(flim.get("percentage") or 0))
        friend_pause = friend_pct >= 80  # D-067/D-069: friend key at 80% (proxy already uses 80), matches proxy LOCK_THRESHOLDS

    state = {
        "ts": int(time.time()),
        "ok": True,
        "session_pct": session_pct,
        "token_pct": token_pct,
        "throttle": throttle,
        "critical": critical,
        "quota_pause": quota_pause,
        "friend_token_pct": friend_pct,
        "friend_pause": friend_pause,
        "session_reset_ms": session_reset,
        "token_reset_ms": token_reset,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))

    # Watchdog: alert only when crossing thresholds
    if not throttle and not friend_pause:
        return 0  # healthy -> silent

    def human(ms):
        if not ms:
            return "?"
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))

    peak_name = "5h SESSION" if session_pct >= token_pct else "TOKENS"
    verb = "CRITICAL — pause scheduler now" if critical else "HIGH — consider pausing scheduler"
    print(f"⚡ Z.AI RATE WATCH ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})")
    print(f"  • {verb}")
    print(f"  • 5h session quota: {session_pct}% used (resets {human(session_reset)})")
    print(f"  • token quota:      {token_pct}% used (resets {human(token_reset)})")
    if quota_pause:
        print(f"  • ⛔ TOKEN QUOTA ≥ 85% — z.ai dispatch PAUSED (D-062). PPQ is ask-first; do NOT auto-failover.")
    if friend_pause:
        print(f"  • 🟡 Friend's fallback key at {friend_pct}% — easing off (D-067, threshold 40%).")
    if session_detail:
        det = session_detail.get("usageDetails") or []
        if det:
            top = ", ".join(f"{m.get('modelCode')}={m.get('usage')}" for m in det[:5])
            print(f"  • top models: {top}")
    print(f"  • state written: {STATE_PATH} (throttle={throttle}) — other jobs should self-skip while True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
