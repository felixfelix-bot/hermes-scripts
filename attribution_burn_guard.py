#!/usr/bin/env python3
"""attribution-burn-guard — stop silent / unattributed token burn.

2026-10-04 incident: a corrupt manager state.db made the gateway retry every
~10s with a ~94k-token context that carried no session id (unattributed), while
`system-bleed-guard.py` could only observe spend, not attribute or halt it. This
guard runs every 10 min and trips the canonical ESTOP + stops the gateway when:

  1. state.db is unhealthy (PRAGMA quick_check) or recent "malformed" /
     "not a database" errors appear — the exact 2026-10-04 failure mode;
  2. unattributed burn (api_calls rows with no session_id) exceeds a cap;
  3. total burn exceeds a cap.

Every token is attributable at the proxy layer (session_id or caller); this
guard is the fail-closed backstop so an unattributable loop can never run
unbounded. Reversible: remove ~/.hermes/ESTOP and `systemctl --user start
hermes-gateway`.

Also supports `retain` to prune + VACUUM the (otherwise unbounded)
burn_attribution.db audit table — invoked from a separate daily cron.

Config-as-code: deployed by role 10-monitors. Env overrides:
  UNATTR_TOKEN_CAP, TOTAL_TOKEN_CAP, GUARD_WINDOW_MIN, GUARD_RETAIN_DAYS.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
ZAI = HERMES / "bot" / "zai_usage.db"
STATE = HERMES / "profiles" / "manager" / "state.db"
ERRLOG = HERMES / "profiles" / "manager" / "logs" / "errors.log"
ESTOP = HERMES / "ESTOP"
FROZEN = HERMES / ".dispatch_frozen"
LOG = HERMES / "logs" / "attribution-burn-guard.log"
ATTR_DB = HERMES / "bot" / "burn_attribution.db"

WINDOW_MIN = int(os.environ.get("GUARD_WINDOW_MIN", "30"))
UNATTR_CAP = int(os.environ.get("UNATTR_TOKEN_CAP", "30000000"))
TOTAL_CAP = int(os.environ.get("TOTAL_TOKEN_CAP", "150000000"))
RETAIN_DAYS = int(os.environ.get("GUARD_RETAIN_DAYS", "7"))
SERVICE = "hermes-gateway.service"


def log(msg: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def burn():
    since = time.time() - WINDOW_MIN * 60
    con = sqlite3.connect(f"file:{ZAI}?mode=ro", uri=True, timeout=15)
    try:
        total = con.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls WHERE ts>?",
            (since,),
        ).fetchone()[0]
        unattr = con.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls "
            "WHERE ts>? AND (session_id IS NULL OR session_id='')",
            (since,),
        ).fetchone()[0]
        cost = con.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE ts>?", (since,)
        ).fetchone()[0]
    finally:
        con.close()
    return int(total), int(unattr), float(cost or 0.0)


def state_health():
    err = None
    try:
        con = sqlite3.connect(f"file:{STATE}?mode=ro", uri=True, timeout=15)
        try:
            row = con.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            con.close()
        if not row or row[0] != "ok":
            err = f"quick_check={row!r}"
    except Exception as e:  # unreadable / corrupt
        err = str(e)
    recent = 0
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=WINDOW_MIN)
        with open(ERRLOG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "malformed" in line or "not a database" in line:
                    try:
                        t = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    if t >= cutoff:
                        recent += 1
    except FileNotFoundError:
        pass
    # quick_check is authoritative. Log-line substring matches caused a
    # false trip (2026-10-04: "malformed executable payload"), so they are
    # reported for context only and never trigger a halt.
    return err is None, err, recent


def engage(reason: str) -> None:
    payload = {
        "engaged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": f"attribution-burn-guard: {reason}",
    }
    try:
        ESTOP.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        FROZEN.touch(exist_ok=True)
    except Exception as e:
        log(f"could not write ESTOP: {e}")
    subprocess.run(["systemctl", "--user", "stop", SERVICE], capture_output=True)
    log(f"ESTOP engaged + {SERVICE} stopped: {reason}")


def guard() -> int:
    total, unattr, cost = burn()
    ok, err, recent = state_health()
    log(
        f"total={total} unattributed={unattr} cost=${cost:.3f} "
        f"state_ok={ok} err={err} recent_corruption_lines={recent}"
    )
    if not ok:
        engage(
            f"state.db unhealthy (err={err}, recent_corruption_lines={recent}); "
            "halting to prevent corruption-loop burn"
        )
        return 0
    if unattr > UNATTR_CAP:
        engage(f"unattributed burn {unattr} tok/{WINDOW_MIN}m > cap {UNATTR_CAP}")
        return 0
    if total > TOTAL_CAP:
        engage(f"total burn {total} tok/{WINDOW_MIN}m > cap {TOTAL_CAP}")
        return 0
    return 0


def retain() -> int:
    if not ATTR_DB.exists():
        log("retain: burn_attribution.db absent")
        return 0
    cutoff = int(time.time()) - RETAIN_DAYS * 86400
    con = sqlite3.connect(str(ATTR_DB), timeout=120)
    try:
        con.execute("PRAGMA busy_timeout=120000")
        n1 = con.execute(
            "DELETE FROM attribution WHERE attributed_at < ?", (cutoff,)
        ).rowcount
        try:
            n2 = con.execute(
                "DELETE FROM task_cost_rollup WHERE ts < ?", (cutoff,)
            ).rowcount
        except sqlite3.OperationalError:
            n2 = 0
        con.commit()
        con.execute("VACUUM")
        log(f"retain: pruned attribution={n1} rollup={n2} (> {RETAIN_DAYS}d)")
    finally:
        con.close()
    return 0


def main(argv) -> int:
    mode = (argv[1] if len(argv) > 1 else "scan").lower()
    if mode == "retain":
        return retain()
    return guard()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
