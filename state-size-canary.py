#!/usr/bin/env python3
"""state-size-canary.py — daily canary for Hermes state growth & corruption.

Silent on success (empty stdout). On any trip, emits ONE aggregated alert
block. Follows the unified-system-alert.sh / --no-agent convention:

    empty stdout = silent, non-empty stdout = delivered verbatim.

Checks (all thresholds env-overridable):

  1. manager/state.db            > 8G       (STATE_MANAGER_LIMIT_BYTES)
  2. any worker-*/state.db       > 1.5G     (STATE_WORKER_LIMIT_BYTES)
  3. df / used%                  > 90       (DISK_USED_PCT_LIMIT)
  4. 24h df-delta of /           > +3G      (DF_DELTA_LIMIT_BYTES)
     — persists the last reading to ~/.hermes/state-df-prev.json
  5. any state.db.corrupted-* file younger than 7d (leak-regression canary)

The df-delta guard only fires when enough time has passed since the last
reading (DF_DELTA_MIN_AGE_SECONDS), so manual back-to-back runs don't
double-count the same growth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"
DF_PREV_PATH = HOME / ".hermes" / "state-df-prev.json"

# thresholds (bytes / percent / seconds)
STATE_MANAGER_LIMIT_BYTES = 8 * 1024**3
STATE_WORKER_LIMIT_BYTES = 1.5 * 1024**3
DISK_USED_PCT_LIMIT = 90.0
DF_DELTA_LIMIT_BYTES = 3 * 1024**3
DF_DELTA_MIN_AGE_SECONDS = 12 * 3600  # ~12h — avoid same-day double-count
CORRUPT_MAX_AGE_SECONDS = 7 * 86400


# --------------------------------------------------------------------------- #
# pure evaluation helpers (explicit inputs => unit-testable)
# --------------------------------------------------------------------------- #
def size_alerts(manager_bytes: float, worker_sizes: dict[str, float]) -> list[str]:
    """Return alert strings for over-limit state.db sizes."""
    alerts = []
    if manager_bytes > STATE_MANAGER_LIMIT_BYTES:
        alerts.append(
            f"manager/state.db is {manager_bytes/1024**3:.1f}G "
            f"(> {STATE_MANAGER_LIMIT_BYTES/1024**3:.0f}G)"
        )
    for name, size in worker_sizes.items():
        if size > STATE_WORKER_LIMIT_BYTES:
            alerts.append(
                f"{name}/state.db is {size/1024**3:.2f}G "
                f"(> {STATE_WORKER_LIMIT_BYTES/1024**3:.1f}G)"
            )
    return alerts


def disk_alerts(used_pct: float) -> list[str]:
    if used_pct > DISK_USED_PCT_LIMIT:
        return [f"/ is {used_pct:.0f}% used (> {DISK_USED_PCT_LIMIT:.0f}%)"]
    return []


def df_delta_alert(prev_used, curr_used: float, prev_ts, now: float) -> bool:
    """True (trip) if / grew > DF_DELTA_LIMIT_BYTES since a sufficiently-old reading."""
    if prev_used is None or prev_ts is None:
        return False
    if now - prev_ts < DF_DELTA_MIN_AGE_SECONDS:
        return False  # not enough elapsed — skip to avoid double-count
    return (curr_used - prev_used) > DF_DELTA_LIMIT_BYTES


def corrupted_alerts(corrupted_files: list[tuple[str, float]], now: float) -> list[str]:
    """Alert on state.db.corrupted-* files younger than CORRUPT_MAX_AGE_SECONDS."""
    out = []
    for path, mtime in corrupted_files:
        if now - mtime < CORRUPT_MAX_AGE_SECONDS:
            out.append(
                f"corrupted state.db artifact: {path} "
                f"({(now - mtime) / 3600:.1f}h old)"
            )
    return out


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def read_df_prev(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "used_bytes" in data:
            return data
    except (OSError, ValueError):
        return None
    return None


def write_df_prev(path: Path, used_bytes: float, ts: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"used_bytes": used_bytes, "ts": ts}))
    except OSError:
        pass  # canary must not crash on unwritable state file


# --------------------------------------------------------------------------- #
# gatherers (real I/O)
# --------------------------------------------------------------------------- #
def manager_db_size(profiles_dir: Path) -> float:
    db = profiles_dir / "manager" / "state.db"
    try:
        return db.stat().st_size
    except OSError:
        return 0.0


def worker_db_sizes(profiles_dir: Path) -> dict[str, float]:
    out = {}
    if not profiles_dir.is_dir():
        return out
    for entry in profiles_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("worker-"):
            db = entry / "state.db"
            try:
                out[entry.name] = db.stat().st_size
            except OSError:
                pass
    return out


def root_disk_usage() -> tuple[float, float]:
    """Return (used_pct, used_bytes) for '/'. Pure stdlib, no df fork."""
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    pct = (used / total) * 100.0 if total else 0.0
    return pct, float(used)


def find_corrupted(profiles_dir: Path) -> list[tuple[str, float]]:
    out = []
    if not profiles_dir.is_dir():
        return out
    for entry in profiles_dir.iterdir():
        if not entry.is_dir():
            continue
        db = entry / "state.db"
        if not db.exists():
            continue
        # look for sibling files named state.db.corrupted-*
        prefix = "state.db.corrupted-"
        try:
            for f in entry.iterdir():
                if f.name.startswith(prefix):
                    out.append((str(f), f.stat().st_mtime))
        except OSError:
            continue
    return out


# Operator-pinned context lengths (mirror of the ctx-governor's
# DELIBERATE_CONTEXT_LENGTHS — deliberately duplicated so a bad governor
# edit can't silently silence the watcher too).
EXPECTED_CONTEXT_LENGTHS: dict[str, int] = {
    "manager": 1_048_576,
}


def context_length_alerts(profiles_dir: Path) -> list[str]:
    out: list[str] = []
    for profile, expected in EXPECTED_CONTEXT_LENGTHS.items():
        config = profiles_dir / profile / "config.yaml"
        if not config.exists():
            out.append(f"context_length[{profile}]: config.yaml missing")
            continue
        try:
            import yaml

            cfg = yaml.safe_load(config.read_text()) or {}
            actual = (cfg.get("model") or {}).get("context_length")
        except Exception as e:
            out.append(f"context_length[{profile}]: unreadable config ({e})")
            continue
        if actual != expected:
            out.append(
                f"context_length[{profile}]: {actual} ≠ pinned {expected} "
                f"— something rewrote it (dashboard pop / stale write)"
            )
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="State-size + corruption canary")
    ap.add_argument("--profiles", type=str, default=str(PROFILES_DIR))
    args = ap.parse_args(argv)

    profiles_dir = Path(args.profiles).expanduser()
    now = time.time()

    alerts: list[str] = []

    # 1+2. state.db sizes
    alerts += size_alerts(manager_db_size(profiles_dir), worker_db_sizes(profiles_dir))

    # 3. disk %
    used_pct, used_bytes = root_disk_usage()
    alerts += disk_alerts(used_pct)

    # 4. df-delta (persist reading)
    prev = read_df_prev(DF_PREV_PATH)
    if prev is not None:
        alerted = df_delta_alert(
            prev.get("used_bytes"), used_bytes, prev.get("ts"), now
        )
        if alerted:
            delta_gb = (used_bytes - prev["used_bytes"]) / 1024**3
            alerts.append(
                f"/ grew {delta_gb:.1f}G in ~24h (> {DF_DELTA_LIMIT_BYTES/1024**3:.0f}G)"
            )
    write_df_prev(DF_PREV_PATH, used_bytes, now)

    # 5. corrupted artifacts
    alerts += corrupted_alerts(find_corrupted(profiles_dir), now)

    # 6. context-length drift (2026-09-05): manager pinned to GLM-5.3's full
    #    1_048_576 window (operator directive). History: an automation loop
    #    (ctx-governor family fallback + usage pollution) silently reverted it
    #    to 200000 repeatedly. The governor now heals drift, this canary is
    #    the independent watcher — it does NOT trust the governor.
    alerts += context_length_alerts(profiles_dir)

    if alerts:
        print("🚨 STATE-SIZE CANARY")
        for a in alerts:
            print(f"   • {a}")
    return 0  # canary never exits nonzero on a trip — the message IS the signal


if __name__ == "__main__":
    sys.exit(main())
