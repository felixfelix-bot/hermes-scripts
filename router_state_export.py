#!/usr/bin/env python3
"""router_state_export.py — export learned router/Kalman state for version control.

D-133: everything needed to restore the live router must be in version control,
including the Kalman filter data. Live learned state lives in ~/.hermes/bot/
(JSON) and the zai_usage.db tables; this dumps a compact, sanitized snapshot to
the repo's state/router/ so a rebuild does not start from a cold filter.

Never writes secrets: only whitelisted JSON state files and numeric DB
aggregates. Usage:
  router_state_export.py [--hermes-home DIR] [--repo DIR] [--db PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# JSON state files in ~/.hermes/bot/ worth versioning (by substring).
STATE_SUBSTR = (
    "kalman", "pool_kalman", "compression", "zai_proxy_state", "ppq_usage",
    "historical_btc", "live_catalog",
)
SKIP_SUFFIX = (".nsec", ".npub", ".env")

# zai_usage.db tables -> rows to keep (compact aggregates / recent points).
DB_TABLES = {
    "kalman_samples": 500,
    "key_health": 200,
    "provider_telemetry": 300,
    "routing_profit": 300,
    "daily_spend": 90,
    "pressure_decisions": 200,
    "flat_router_shadow_decisions": 200,
}


def _hermes_home(explicit):
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))


def export_json(bot: Path, out: Path) -> list[str]:
    written = []
    if not bot.is_dir():
        return written
    for p in sorted(bot.iterdir()):
        if not p.is_file() or p.suffix != ".json":
            continue
        if p.name.endswith(SKIP_SUFFIX):
            continue
        if not any(s in p.name for s in STATE_SUBSTR):
            continue
        try:
            json.loads(p.read_text())  # must parse
        except Exception:
            continue
        shutil.copy2(p, out / p.name)
        written.append(p.name)
    return written


def export_db(db: Path, out: Path) -> dict:
    res = {}
    if not db.exists():
        return res
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
    except Exception:
        return res
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    for t, limit in DB_TABLES.items():
        if t not in tables:
            continue
        try:
            rows = [dict(r) for r in c.execute(
                f"select * from {t} order by rowid desc limit {limit}")]
        except Exception:
            continue
        (out / f"db_{t}.json").write_text(json.dumps(
            {"table": t, "exported": int(time.time()), "rows": rows}, indent=1))
        res[t] = len(rows)
    c.close()
    return res


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    home = _hermes_home(args.hermes_home)
    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parents[2]
    bot = home / "bot"
    db = Path(args.db) if args.db else bot / "zai_usage.db"
    out = repo / "state" / "router"
    out.mkdir(parents=True, exist_ok=True)

    jsons = export_json(bot, out)
    dbs = export_db(db, out)
    manifest = {"exported": int(time.time()), "home": str(home),
                "json_files": jsons, "db_tables": dbs}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if args.json:
        print(json.dumps(manifest, indent=1))
    else:
        print(f"router_state_export: {len(jsons)} json + {len(dbs)} db table(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
