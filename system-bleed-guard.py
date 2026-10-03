#!/usr/bin/env python3
"""system-bleed-guard.py — Automatic token-bleed detection + remediation.

Detects the SYSTEM-level failure modes that drove the 2026-09-08 token-bleed
incident (1.92B tokens/24h) and remediates them automatically, following the
same design contract as circuit_breaker.py (fail-open, persistent state,
staged log-only -> enforce, operator-only reset).

What it watches (2 consecutive ticks to confirm each):
  1. Burn burst      — tokens/hr or spend/hr above threshold
  2. Dispatch runaway — running workers > max_in_progress + margin
  3. Swap thrashing  — low free RAM AND high swap AND high load
  4. Context bloat   — avg prompt_tokens/call above threshold (flag only)

Two-tier remediation:
  Tier 1 (soft):  engage global freeze (write ~/.hermes/ESTOP, the sentinel
                  the gateway dispatcher + cron scheduler already honor) +
                  touch ~/.hermes/bot/.dispatch_frozen + kill workers beyond
                  max_in_progress (keep the oldest).
  Tier 2 (hard):  still breaching 3 ticks after Tier 1 -> kill ALL workers +
                  crash-wrappers + stale tsserver LSPs, restart zai-proxy.
  Recover:        3 clean ticks -> disengage freeze + notify.

Notifications go through the existing anomaly_events -> anomaly-notify.sh
pipeline (same Signal channel as all other alerts).

Fail-open everywhere: any unexpected error logs and exits 0 (never wedges
dispatch). The sentinel/flag writes are the authoritative freeze; this script
never *blocks* dispatch on its own failure.

Usage:
  system-bleed-guard.py scan [--mode log-only|enforce]   # cron pass (5 min)
  system-bleed-guard.py status [--json]                  # print state + detector snapshot
  system-bleed-guard.py freeze-now [--reason ...]        # operator: engage freeze now
  system-bleed-guard.py unfreeze                         # operator: lift freeze + reset state

Mode is read from BLEED_GUARD_MODE (default "enforce") or --mode.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
HERMES = HOME / ".hermes"
BOT = HERMES / "bot"
SCRIPTS = HERMES / "scripts"
STATE_FILE = BOT / "bleed_guard_state.json"
ESTOP = HERMES / "ESTOP"                       # canonical freeze sentinel (agent/estop.py)
DISPATCH_FROZEN = BOT / ".dispatch_frozen"     # legacy flag (staggered-dispatch.sh)
QUARANTINE = BOT / ".fleet_quarantine"          # Phase-H hard hold (no auto-resume until healthy)
OPERATOR_HOLD = BOT / ".spend_hold"            # operator hold: never auto-unfreeze while present
USAGE_DB = BOT / "zai_usage.db"
CONFIG = HERMES / "config.yaml"

# ── Thresholds. 2026-10-03 re-tune after the paid-DeepSeek burn: a topped-up
#    lane was drained ~$13-27/day by unattended background retries. The guard
#    was running in log-only mode (crontab) AND spend/hr was $6, so it never
#    fired. Now: real-money hourly cap at $2/hr plus a hard daily ceiling. Tokens
#    are secondary (deepseek-flash is cheap, so raw token volume alone is not
#    "waste" — spend is). ────────────────────────────────────────────────────
BURN_TOKENS_PER_HOUR = 80_000_000   # tokens/hr (secondary)
BURN_SPEND_PER_HOUR = 2.0           # USD/hr (PRIMARY — real money waste)
BURN_DAILY_SPEND_MAX = 10.0         # USD/day ceiling across all lanes (0 disables)
RUNAWAY_MARGIN = 3                  # running > max_in_progress + this
FREE_RAM_MB_MIN = 500               # below this is "memory starved"
SWAP_MB_MAX = 5000                  # above this is "swap thrashing"
LOAD_FACTOR = 1.5                   # load1 > this * nproc is "overloaded"
BLOAT_PROMPT_TOKENS = 150_000       # avg prompt_tokens/call (flag only)

CONFIRM_TICKS = 1                   # OPERATOR 2026-09-11 (H.7): freeze on FIRST breach tick
HARD_AFTER_TICKS = 5                # breaching ticks to escalate Tier 2 (4 after Tier 1)
RECOVER_TICKS = 3                   # clean ticks to auto-recover
HARD_COOLDOWN_S = 30 * 60           # don't re-run hard kill within 30 min
BURN_WINDOW_S = 900                # 15 min window for burn rate (was 1h — too slow to clear after a burst)
BLOAT_WINDOW_S = 3600               # 1h window for bloat avg

# Auto-quarantine (OPERATOR 2026-09-11, H.7): if a node storm-cycles repeatedly,
# stop auto-resuming (the refill loop) and hold until a sustained healthy window
# clears it automatically. Accepted as a BOUNDED override of "auto-resume".
QUARANTINE_CYCLES = 4               # storm cycles within the window that trigger quarantine
QUARANTINE_WINDOW_S = 6 * 3600      # rolling window for cycle counting
QUARANTINE_CLEAR_TICKS = 12         # clean ticks (60 min) required to auto-clear quarantine
EFF_WASTE_RATIO = 3.0               # spend-per-output vs trailing median that counts as waste

# ── DB helpers ────────────────────────────────────────────────────────────────

def _usagedb():
    if not USAGE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _read_config_max_in_progress() -> int:
    """Best-effort read of kanban.max_in_progress; default 3 on any failure."""
    try:
        txt = CONFIG.read_text(encoding="utf-8")
    except Exception:
        return 3
    m = re.search(r"^\s*max_in_progress:\s*(\d+)\s*$", txt, re.MULTILINE)
    if not m:
        return 3
    try:
        return int(m.group(1))
    except ValueError:
        return 3


# ── System detectors ──────────────────────────────────────────────────────────

def _nproc() -> int:
    try:
        return int(subprocess.check_output(["nproc"], text=True).strip())
    except Exception:
        return 4


def _mem_mb():
    """Return (mem_available_mb, swap_used_mb) from /proc/meminfo."""
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])  # kB
    except Exception:
        return (None, None)
    avail = info.get("MemAvailable", info.get("MemFree", None))
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return (
        None if avail is None else avail // 1024,
        swap_used // 1024,
    )


def _load1() -> float:
    try:
        return float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except Exception:
        return 0.0


def _etimes(pid: int) -> int:
    """Approximate elapsed seconds for a pid (from /proc starttime)."""
    try:
        clk = int(os.sysconf("SC_CLK_TCK") or 100)
        start = int(Path(f"/proc/{pid}/stat").read_text().split()[21])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return max(0, int(uptime - start / clk))
    except Exception:
        return 0


def _worker_processes():
    """Return list of (pid, etimes) for running workers.

    OPERATOR 2026-09-11 (I.2 / RC-12): the authoritative source is the admission
    slot directory (one live PID per admitted worker). The old pgrep counted
    BOTH the wrapper and its hermes child (≈2×) and could false-match unrelated
    command lines. Falls back to pgrep only when no slots are present.
    """
    out: list[tuple[int, int]] = []
    slots = BOT / ".fleet_slots"
    if slots.is_dir():
        for f in slots.glob("*.slot"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            except PermissionError:
                pass
            out.append((pid, _etimes(pid)))
        if out:
            return out
    # Fallback (no admission slots in use): ps-based, deduped by pid.
    try:
        raw = subprocess.check_output(
            ["ps", "-eo", "pid=,etimes=,args="], text=True
        )
    except Exception:
        return out
    seen: set[int] = set()
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, et, args = parts[0], parts[1], parts[2]
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid in seen:
            continue
        if "kanban-crash-wrapper" in args or " -p worker" in args:
            try:
                out.append((pid, int(et)))
                seen.add(pid)
            except ValueError:
                continue
    return out


def _tsserver_processes():
    out = []
    try:
        raw = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    except Exception:
        return out
    for line in raw.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        if "tsserver.js" in parts[1] or "pyright-langserver" in parts[1]:
            try:
                out.append(int(parts[0]))
            except ValueError:
                continue
    return out


def _check_burn(conn):
    """Return (breached, detail) for burn burst."""
    if conn is None:
        return (False, "no usage db")
    cutoff = time.time() - BURN_WINDOW_S
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) tok, COALESCE(SUM(cost_usd),0) cost "
            "FROM api_calls WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    except Exception as exc:
        return (False, f"burn query error: {exc}")
    tok = int(row["tok"] or 0)
    cost = float(row["cost"] or 0.0)
    tok_per_h = tok / (BURN_WINDOW_S / 3600.0)
    detail = f"{tok_per_h/1e6:.1f}M tok/hr, ${cost:.2f}/hr"
    breached = tok_per_h > BURN_TOKENS_PER_HOUR or cost > BURN_SPEND_PER_HOUR
    return (breached, detail)


def _check_daily_spend(conn):
    """Return (breached, detail) for the hard daily spend ceiling.

    2026-10-03: a slow steady burn below the hourly cap still drained a topped-up
    paid lane over a day ($20-55/day booked). This catches the cumulative case.
    Uses the daily_spend rollup; a missing/stale table is a no-op (never wedges).
    """
    if conn is None or BURN_DAILY_SPEND_MAX <= 0:
        return (False, "no usage db")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(spend_usd),0) s, COALESCE(SUM(call_count),0) n "
            "FROM daily_spend WHERE date = ?",
            (today,),
        ).fetchone()
    except Exception as exc:
        return (False, f"daily query error: {exc}")
    spent = float(row["s"] or 0.0)
    n = int(row["n"] or 0)
    detail = f"today ${spent:.2f} over {n} calls (cap ${BURN_DAILY_SPEND_MAX:.0f})"
    return (spent > BURN_DAILY_SPEND_MAX, detail)


def _check_runaway(max_in_progress):
    """Return (breached, detail) for dispatch runaway."""
    workers = _worker_processes()
    n = len(workers)
    limit = max_in_progress + RUNAWAY_MARGIN
    return (n > limit, f"{n} workers vs max_in_progress={max_in_progress} (+{RUNAWAY_MARGIN} margin)")


def _check_thrashing():
    """Return (breached, detail) for swap thrashing."""
    avail, swap = _mem_mb()
    load = _load1()
    ncpu = _nproc()
    detail = f"free={avail}MB swap={swap}MB load={load:.2f} (nproc={ncpu})"
    if avail is None:
        return (False, detail)
    breached = avail < FREE_RAM_MB_MIN and swap > SWAP_MB_MAX and load > LOAD_FACTOR * ncpu
    return (breached, detail)


def _check_efficiency():
    """Return (breached, detail) from fleet_health.json's waste_ratio (fresh only).

    OPERATOR 2026-09-11 (H.7): the outcome-relative detector — spend rate divided
    by the trailing-median output rate (kanban completions + PR merges), computed
    by fleet_health.py (H.9). Absent/stale signal is a no-op so this cannot wedge
    the guard before H.9 ships.
    """
    p = BOT / "fleet_health.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return (False, "no fleet_health")
    try:
        age = time.time() - float(data.get("ts", 0) or 0)
    except (TypeError, ValueError):
        return (False, "fleet_health bad ts")
    if age > 1800:
        return (False, f"fleet_health stale ({int(age)}s)")
    wr = data.get("waste_ratio")
    if wr is None:
        return (False, "no waste_ratio")
    try:
        wrf = float(wr)
    except (TypeError, ValueError):
        return (False, "bad waste_ratio")
    detail = f"waste_ratio={wrf:.2f} ({data.get('waste_reason', 'spend vs output')})"
    return (wrf >= EFF_WASTE_RATIO, detail)


def _check_bloat(conn):
    """Return (flagged, detail) for context bloat."""
    if conn is None:
        return (False, "no usage db")
    cutoff = time.time() - BLOAT_WINDOW_S
    try:
        row = conn.execute(
            "SELECT COALESCE(AVG(prompt_tokens),0) avg, COUNT(*) n "
            "FROM api_calls WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    except Exception as exc:
        return (False, f"bloat query error: {exc}")
    avg = int(row["avg"] or 0)
    n = int(row["n"] or 0)
    detail = f"avg prompt {avg/1e3:.0f}k tokens/call ({n} calls/1h)"
    return (avg > BLOAT_PROMPT_TOKENS and n >= 5, detail)


# ── Actions ───────────────────────────────────────────────────────────────────

def _freeze_engage(reason: str):
    """Engage global freeze: write ESTOP sentinel + legacy dispatch_frozen flag."""
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    try:
        ESTOP.parent.mkdir(parents=True, exist_ok=True)
        ESTOP.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        try:
            ESTOP.touch(exist_ok=True)
        except OSError:
            pass
    try:
        DISPATCH_FROZEN.parent.mkdir(parents=True, exist_ok=True)
        DISPATCH_FROZEN.touch(exist_ok=True)
    except OSError:
        pass


def _freeze_disengage():
    for p in (ESTOP, DISPATCH_FROZEN):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _quarantine_engage(reason: str):
    _freeze_engage(reason)  # quarantine implies a freeze
    try:
        QUARANTINE.parent.mkdir(parents=True, exist_ok=True)
        QUARANTINE.write_text(json.dumps({
            "engaged_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }, indent=2) + "\n", encoding="utf-8")
    except OSError:
        try:
            QUARANTINE.touch(exist_ok=True)
        except OSError:
            pass


def _quarantine_disengage():
    try:
        QUARANTINE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _is_quarantined() -> bool:
    try:
        return QUARANTINE.exists()
    except OSError:
        return False


def _is_frozen() -> bool:
    return ESTOP.exists() or DISPATCH_FROZEN.exists()


def _is_operator_hold() -> bool:
    """True while an operator has pinned the freeze (no auto-unfreeze).

    2026-10-03: inserted so a hand-engaged freeze (hermes pause / spend_hold)
    can never be silently lifted by the recovery path, which previously refilled
    dispatch after 3 clean ticks and resumed the paid-lane burn.
    """
    try:
        return OPERATOR_HOLD.exists()
    except OSError:
        return False


def _kill_pids(pids, sig="TERM"):
    for pid in pids:
        try:
            os.kill(pid, getattr(__import__("signal"), f"SIG{sig}"))
        except (ProcessLookupError, PermissionError):
            continue


def _kill_excess_workers(max_in_progress):
    """Kill youngest workers so only max_in_progress remain (keep oldest)."""
    workers = _worker_processes()  # list of (pid, etimes), etimes = elapsed seconds
    if len(workers) <= max_in_progress:
        return 0
    # youngest first (smallest etimes)
    workers.sort(key=lambda x: x[1])
    excess = workers[: len(workers) - max_in_progress]
    _kill_pids([pid for pid, _ in excess])
    return len(excess)


def _hard_kill():
    """Kill ALL workers, wrappers, and stale LSPs; restart zai-proxy."""
    workers = _worker_processes()
    lsp = _tsserver_processes()
    _kill_pids([pid for pid, _ in workers], "KILL")
    _kill_pids(lsp, "KILL")
    # restart proxy (reuse the same env the existing restart_proxy.sh uses)
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "zai-proxy.service"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        proxy_restarted = True
    except Exception:
        proxy_restarted = False
    return len(workers), len(lsp), proxy_restarted


# ── Anomaly notify (existing pipeline) ────────────────────────────────────────

def _notify(severity, title, detail):
    """Insert into anomaly_events; anomaly-notify.sh picks it up."""
    try:
        conn = sqlite3.connect(str(USAGE_DB), timeout=5)
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail) "
            "VALUES (?, ?, 'bleed-guard', ?, ?)",
            (time.time(), severity, title, detail),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(mode="enforce"):
    conn = _usagedb()
    max_in_progress = _read_config_max_in_progress()

    burn_b, burn_d = _check_burn(conn)
    run_b, run_d = _check_runaway(max_in_progress)
    thr_b, thr_d = _check_thrashing()
    eff_b, eff_d = _check_efficiency()
    bloat_b, bloat_d = _check_bloat(conn)
    day_b, day_d = _check_daily_spend(conn)

    severe = burn_b or run_b or thr_b or eff_b or day_b
    state = _load_state()
    now = time.time()
    breach_streak = int(state.get("breach_streak", 0))
    clean_streak = int(state.get("clean_streak", 0))
    tier = int(state.get("tier", 0))
    last_hard_ts = float(state.get("last_hard_ts", 0))
    last_bloat_notify = float(state.get("last_bloat_notify", 0))
    storm_cycles = [t for t in (state.get("storm_cycles") or [])
                    if now - float(t) <= QUARANTINE_WINDOW_S]

    actions = []

    if severe:
        breach_streak += 1
        clean_streak = 0
        symptoms = [d for b, d in ((burn_b, f"burn {burn_d}"),
                                   (day_b, f"daily {day_d}"),
                                   (run_b, f"runaway {run_d}"),
                                   (thr_b, f"thrash {thr_d}"),
                                   (eff_b, f"efficiency {eff_d}")) if b]
        reason = "bleed-guard: " + "; ".join(symptoms)

        if breach_streak >= CONFIRM_TICKS and not _is_frozen():
            # Tier 1 — soft freeze ONLY. Freeze NEW dispatch; do NOT kill
            # in-flight workers. The Kalman headroom governor already bounds
            # spawns and holds before pressure, so killing here is redundant
            # and harmful: it SIGTERMs workers mid-task, which trips the
            # circuit-breaker and permanently blocks their tasks. Killing is
            # a last resort reserved for Tier 2.
            if mode == "enforce":
                _freeze_engage(reason)
                _notify("warning", "bleed-guard: soft freeze engaged",
                        f"{reason}; new dispatch held (in-flight workers left to finish)")
                actions.append("SOFT freeze (new dispatch held)")
            else:
                actions.append(f"WOULD soft-freeze ({reason})")
            tier = 1
            # Storm-cycle accounting -> auto-quarantine after repeated cycles.
            storm_cycles.append(now)
            if len(storm_cycles) >= QUARANTINE_CYCLES and not _is_quarantined():
                qreason = (f"{len(storm_cycles)} storm cycles in "
                           f"{QUARANTINE_WINDOW_S // 3600}h — auto-quarantine")
                if mode == "enforce":
                    _quarantine_engage(reason + " | " + qreason)
                    _notify("critical", "bleed-guard: QUARANTINE engaged",
                            f"{reason}; {qreason}")
                    actions.append(f"QUARANTINE ({len(storm_cycles)} cycles/"
                                   f"{QUARANTINE_WINDOW_S // 3600}h)")
                else:
                    actions.append(f"WOULD quarantine ({qreason})")

        elif breach_streak >= HARD_AFTER_TICKS and tier >= 1:
            # Tier 2 — hard kill (cooldown-guarded)
            if now - last_hard_ts >= HARD_COOLDOWN_S:
                if mode == "enforce":
                    nw, nl, pr = _hard_kill()
                    _notify("critical", "bleed-guard: hard kill",
                            f"{reason}; killed {nw} workers, {nl} LSPs, proxy_restart={pr}")
                    actions.append(f"HARD kill ({nw} workers, {nl} LSPs, proxy={pr})")
                else:
                    actions.append(f"WOULD hard-kill ({reason})")
                last_hard_ts = now
            tier = 2

    else:
        clean_streak += 1
        breach_streak = 0
        if _is_frozen():
            if _is_quarantined():
                # Quarantine: only a sustained healthy window clears it (H.7).
                if clean_streak >= QUARANTINE_CLEAR_TICKS:
                    if _is_operator_hold():
                        actions.append("HOLD (operator; quarantine kept)")
                    elif mode == "enforce":
                        _quarantine_disengage()
                        _freeze_disengage()
                        _notify("info", "bleed-guard: quarantine cleared",
                                f"{QUARANTINE_CLEAR_TICKS} clean ticks; freeze lifted")
                        actions.append("QUARANTINE cleared (auto)")
                    else:
                        actions.append("WOULD clear quarantine")
                    if not _is_operator_hold():
                        tier = 0
                        clean_streak = 0
                        storm_cycles = []
            elif clean_streak >= RECOVER_TICKS:
                # 2026-10-03: an operator hold pins the freeze so recovery cannot
                # silently refill dispatch and resume a paid-lane burn.
                if _is_operator_hold():
                    actions.append("HOLD (operator .spend_hold present; not auto-unfreezing)")
                elif mode == "enforce":
                    _freeze_disengage()
                    _notify("info", "bleed-guard: recovered", "3 clean ticks; freeze lifted")
                    actions.append("RECOVER (unfroze)")
                else:
                    actions.append("WOULD recover (unfreeze)")
                if not _is_operator_hold():
                    tier = 0
                    clean_streak = 0

    # Context bloat — flag only, dedup'd (max once per 6h)
    if bloat_b and now - last_bloat_notify >= 6 * 3600:
        _notify("info", "bleed-guard: context bloat",
                f"avg prompt tokens high: {bloat_d}")
        last_bloat_notify = now
        actions.append(f"FLAG bloat ({bloat_d})")

    state.update({
        "breach_streak": breach_streak,
        "clean_streak": clean_streak,
        "tier": tier,
        "last_hard_ts": last_hard_ts,
        "last_bloat_notify": last_bloat_notify,
        "last_run": now,
        "mode": mode,
        "storm_cycles": storm_cycles,
        "quarantined": _is_quarantined(),
        "last_detectors": {
            "burn": burn_d, "daily": day_d, "runaway": run_d,
            "thrash": thr_d, "efficiency": eff_d, "bloat": bloat_d,
        },
        "frozen": _is_frozen(),
    })
    _save_state(state)

    if conn is not None:
        conn.close()

    if actions or severe:
        summary = "; ".join(actions) if actions else f"breaching (streak={breach_streak})"
        print(f"[{datetime.now(timezone.utc).isoformat()}] severe={severe} "
              f"({burn_d} | {run_d} | {thr_d}) -> {summary}")
    return 0


def status(as_json=False):
    state = _load_state()
    conn = _usagedb()
    max_in_progress = _read_config_max_in_progress()
    burn_b, burn_d = _check_burn(conn)
    run_b, run_d = _check_runaway(max_in_progress)
    thr_b, thr_d = _check_thrashing()
    eff_b, eff_d = _check_efficiency()
    bloat_b, bloat_d = _check_bloat(conn)
    day_b, day_d = _check_daily_spend(conn)
    if conn is not None:
        conn.close()
    snap = {
        "version": VERSION,
        "frozen": _is_frozen(),
        "operator_hold": _is_operator_hold(),
        "quarantined": _is_quarantined(),
        "state": state,
        "detectors": {
            "burn": burn_d, "daily": day_d, "runaway": run_d, "thrash": thr_d,
            "efficiency": eff_d, "bloat": bloat_d,
        },
    }
    if as_json:
        print(json.dumps(snap, indent=2))
    else:
        print(f"bleed-guard v{VERSION}  frozen={snap['frozen']} "
              f"operator_hold={snap['operator_hold']} quarantined={snap['quarantined']}")
        print(f"  burn:       {burn_d} {'<-- BREACH' if burn_b else ''}")
        print(f"  daily:      {day_d} {'<-- BREACH' if day_b else ''}")
        print(f"  runaway:    {run_d} {'<-- BREACH' if run_b else ''}")
        print(f"  thrash:     {thr_d} {'<-- BREACH' if thr_b else ''}")
        print(f"  efficiency: {eff_d} {'<-- BREACH' if eff_b else ''}")
        print(f"  bloat:      {bloat_d} {'<-- FLAG' if bloat_b else ''}")
        st = state.get("breach_streak", 0)
        cl = state.get("clean_streak", 0)
        print(f"  streak:     breach={st} clean={cl} tier={state.get('tier', 0)} "
              f"storm_cycles={len(state.get('storm_cycles') or [])}")
    return 0


def freeze_now(reason):
    _freeze_engage(reason or "bleed-guard: operator freeze-now")
    _notify("warning", "bleed-guard: operator freeze", reason or "manual freeze-now")
    print(f"engaged: ESTOP + .dispatch_frozen (reason: {reason or 'manual'})")
    return 0


def unfreeze():
    _freeze_disengage()
    _save_state({})  # reset state on manual unfreeze
    _notify("info", "bleed-guard: operator unfreeze", "manual unfreeze")
    print("disengaged: removed ESTOP + .dispatch_frozen; state reset")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Automatic token-bleed guard")
    ap.add_argument("command", nargs="?", default="scan",
                    choices=("scan", "status", "freeze-now", "unfreeze"))
    ap.add_argument("--mode", default=os.environ.get("BLEED_GUARD_MODE", "enforce"),
                    choices=("log-only", "enforce"))
    ap.add_argument("--reason", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        if args.command == "scan":
            return scan(mode=args.mode)
        if args.command == "status":
            return status(as_json=args.json)
        if args.command == "freeze-now":
            return freeze_now(args.reason)
        if args.command == "unfreeze":
            return unfreeze()
    except Exception as exc:  # fail-open: never wedge dispatch
        print(f"bleed-guard: internal error (fail-open): {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
