#!/usr/bin/env python3
"""fleet_freeze_guard.py — break the dispatch-freeze loop (D-128 §15.2).

The arbiter can freeze dispatch (`.dispatch_frozen`) on a storm signal. When
that signal is noisy the node re-freezes repeatedly, silently stalling every
cron and worker. Operator policy (2026-09-13): abnormal burn is ALERTS ONLY;
freeze is reserved for genuine runaway — so a freeze LOOP is a defect.

This guard:
  * FAIL-OPEN auto-clears a **stale** freeze: when the marker carries an
    `expires_at` in the past (or, for legacy markers, is older than the TTL from
    `freeze_policy.json`, default 1800s), `ESTOP` / `.dispatch_frozen` /
    `.fleet_offload_disabled` are removed and an alert is emitted. A hard
    load/cpu spike that has since cleared must not latch dispatch forever
    (2026-09-21 stale-freeze incident). A `.fleet_quarantine` held by L6 is
    never auto-lifted. BOTH the canonical `ESTOP` sentinel AND the legacy
    `.dispatch_frozen` flag count as "a freeze is present" — an ESTOP-only
    freeze previously slipped past this guard because it only looked at
    `.dispatch_frozen`, so an expired sim sentinel held every spawn for hours
    while the guard reported nothing (2026-09-21);
  * counts freeze/quarantine actions in the last `window` seconds from
    `fleet_interventions.jsonl`; when the count clears `threshold` (default
    6/24h) AND a freeze marker is present, FAIL-OPEN auto-clears and alerts;
  * silent (empty stdout) when there is no freeze loop and no stale marker.

Usage: fleet_freeze_guard.py [--threshold N] [--window S] [--ttl S] [--no-clear] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
LEDGER = BOT / "fleet_interventions.jsonl"
USAGE_DB = BOT / "zai_usage.db"
FROZEN = BOT / ".dispatch_frozen"
OFFLOAD_DISABLED = BOT / ".fleet_offload_disabled"
QUARANTINE = BOT / ".fleet_quarantine"
ESTOP = HERMES / "ESTOP"
POLICY = BOT / "freeze_policy.json"
STATE = HERMES / "state" / "freeze_guard.json"
FREEZE_ACTIONS = ("freeze", "quarantine")
DEFAULT_THRESHOLD = 6
DEFAULT_WINDOW = 86400
DEFAULT_TTL = 1800


def _policy_ttl() -> int:
    """Freeze TTL (seconds) from the version-controlled policy, 0 = sticky."""
    try:
        return max(0, int((json.loads(POLICY.read_text()) or {}).get("ttl_s", DEFAULT_TTL)))
    except Exception:  # noqa: BLE001
        return DEFAULT_TTL


def stale_reason(now: float, ttl: int, marker_present: bool) -> str | None:
    """Why a present freeze marker is stale, or None if it is still fresh.

    Prefers the `expires_at` written by fleet_remediate; falls back to the
    marker's age for legacy/sticky markers. ttl<=0 disables the check.
    """
    if not marker_present or ttl <= 0:
        return None
    expires = None
    try:
        e = json.loads(ESTOP.read_text())
        if isinstance(e, dict):
            expires = e.get("expires_at")
    except Exception:  # noqa: BLE001
        expires = None
    if expires is not None:
        try:
            return (f"freeze expired {int(now - float(expires))}s ago"
                    if float(expires) <= now else None)
        except (TypeError, ValueError):
            pass
    src = ESTOP if ESTOP.exists() else FROZEN
    try:
        age = now - src.stat().st_mtime
    except OSError:
        return None
    return f"freeze marker age {int(age)}s > ttl {ttl}s" if age > ttl else None


def count_actions(ledger: Path, window: int, now: float) -> int:
    n = 0
    try:
        for line in ledger.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if e.get("action") in FREEZE_ACTIONS and now - float(e.get("ts", 0)) <= window:
                n += 1
    except OSError:
        pass
    return n


def should_clear(count: int, threshold: int, marker_present: bool) -> bool:
    return marker_present and count >= threshold


def _alert(detail: str) -> None:
    try:
        c = sqlite3.connect(str(USAGE_DB), timeout=5)
        c.execute("INSERT INTO anomaly_events (ts, severity, category, title, detail) "
                  "VALUES (?,?,?,?,?)",
                  (time.time(), "WARN", "freeze-loop",
                   "dispatch freeze loop auto-cleared", detail))
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _clear_markers() -> list[str]:
    errs: list[str] = []
    for p in (ESTOP, FROZEN, OFFLOAD_DISABLED):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            errs.append(f"FAILED to clear {p}: {exc}")
    return errs


def _write_state(now: float, count: int, kind: str, detail: str | None) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last_clear": int(now), "count": count,
                                 "kind": kind, "detail": detail}, indent=1))


def run(threshold: int, window: int, clear: bool, now: float | None = None,
        ttl: int | None = None) -> list[str]:
    now = now if now is not None else time.time()
    ttl = _policy_ttl() if ttl is None else ttl
    count = count_actions(LEDGER, window, now)
    msgs: list[str] = []
    # The canonical ESTOP sentinel AND the legacy .dispatch_frozen flag both
    # mean "freeze present". Gating only on FROZEN let an ESTOP-only expired
    # freeze hold every spawn invisibly (2026-09-21).
    marker = FROZEN.exists() or ESTOP.exists()
    quarantined = QUARANTINE.exists()
    stale = stale_reason(now, ttl, marker)
    if clear and marker and stale and not quarantined:
        msgs.extend(_clear_markers())
        msgs.append(f"STALE-FREEZE auto-cleared: {stale}; dispatch unfrozen")
        _alert(f"stale freeze ({stale}); cleared {FROZEN.name} fail-open "
               f"(ttl={ttl}s)")
        _write_state(now, count, "stale", stale)
    elif clear and should_clear(count, threshold, marker):
        msgs.extend(_clear_markers())
        msgs.append(f"FREEZE-LOOP auto-cleared: {count} freeze actions/{window}s "
                    f">= {threshold}; dispatch unfrozen")
        _alert(f"{count} freeze actions in {window}s (threshold {threshold}); "
               f"cleared {FROZEN.name} fail-open")
        _write_state(now, count, "loop", None)
    elif stale and quarantined:
        msgs.append(f"STALE-FREEZE alert: {stale} "
                    f"(quarantine marker present; not auto-lifted)")
    elif count >= threshold:
        msgs.append(f"FREEZE-LOOP alert: {count} freeze actions/{window}s "
                    f">= {threshold} (no marker to clear)")
    return msgs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--ttl", type=int, default=None,
                    help="freeze TTL seconds (0=sticky); default from freeze_policy.json")
    ap.add_argument("--no-clear", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    msgs = run(args.threshold, args.window, not args.no_clear, ttl=args.ttl)
    if args.json:
        print(json.dumps({"messages": msgs}))
    else:
        for m in msgs:
            print(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
