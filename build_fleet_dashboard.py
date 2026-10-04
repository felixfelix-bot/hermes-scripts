#!/usr/bin/env python3
"""build_fleet_dashboard.py — render a per-node Hermes fleet dashboard.

Aggregates the data the nodes already publish (fleet_health.json + peers/*.json,
which now include the Phase-S `components{}` block) plus per-node task throughput
from the board DBs, into a single self-contained HTML page (deployable as an
nsite) and/or JSON.

Data path today: peers push `bot/peers/<node>.health.json` over SSH, so the
controller sees the whole fleet. (ContextVM/MCP is the alternative live
transport; the fields are the same.)

Usage:
  build_fleet_dashboard.py [--bot PATH] [--boards PATH] [--out FILE] [--json]
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BOT = HERMES / "bot"
BOARDS = HERMES / "kanban" / "boards"
PEERS = BOT / "peers"


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def collect(bot: Path = BOT) -> list[dict]:
    """Return one merged record per node (self + peers)."""
    nodes: dict[str, dict] = {}
    # Self first, then peers (peers/*.health.json is richer than *.json).
    for pat in ("fleet_health.json", "peers/*.health.json", "peers/*.json"):
        for f in sorted(glob.glob(str(bot / pat))):
            d = _read_json(Path(f))
            if not isinstance(d, dict) or not d.get("node"):
                continue
            node = d["node"]
            if node not in nodes:
                nodes[node] = d
            else:
                nodes[node] = {**nodes[node], **d}
    return [{"node": k, **v} for k, v in sorted(nodes.items())]


def _host_node_map() -> dict:
    """Map OS hostname (lowercased) -> fleet node id from state/fleet/nodes.json."""
    repo = Path(__file__).resolve().parent.parent.parent
    data = _read_json(repo / "state" / "fleet" / "nodes.json") or {}
    out = {}
    for n in data.get("nodes", []):
        node = n.get("id")
        for key in (n.get("hostname"), n.get("id")):
            if key:
                out[str(key).lower()] = node
    return out


def throughput(boards: Path = BOARDS, window_s: int = 86400, host_map: dict | None = None) -> dict:
    """tasks completed per fleet node in the last `window_s`.

    `task_runs.metadata.claimer` is `<hostname>:<pid>`; normalise the hostname to
    the fleet node id so the dashboard can attribute throughput per node.
    """
    out: dict[str, int] = {}
    host_map = host_map if host_map is not None else _host_node_map()
    now = time.time()
    for db in glob.glob(str(boards / "*" / "kanban.db")):
        try:
            c = sqlite3.connect(db, timeout=3)
            cur = c.execute("SELECT metadata, status, ended_at FROM task_runs")
            for meta, status, ended in cur:
                if status not in ("completed", "done"):
                    continue
                if ended and (now - float(ended)) > window_s:
                    continue
                try:
                    claimer = json.loads(meta or "{}").get("claimer", "")
                except Exception:
                    claimer = ""
                host = claimer.split(":")[0].lower() if claimer else ""
                node = host_map.get(host) or (host or "unattributed")
                out[node] = out.get(node, 0) + 1
            c.close()
        except Exception:
            continue
    return out


def _pct(a, b) -> float:
    try:
        return round(100.0 * float(a) / float(b), 1) if b else 0.0
    except Exception:
        return 0.0


def render(nodes: list[dict], tput: dict | None = None) -> str:
    tput = tput or {}
    rows = []
    for n in nodes:
        node = html.escape(str(n.get("node", "?")))
        comp = n.get("components", {}) or {}
        badges = []
        for cname in ("gateway", "workers", "kanban_sync", "responder", "fips", "timers"):
            st = (comp.get(cname) or {}).get("status", "?")
            cls = {"ok": "ok", "degraded": "warn", "down": "bad"}.get(st, "na")
            badges.append(f"<span class='badge {cls}' title='{html.escape(cname)}'>{html.escape(cname[:3])}</span>")
        mem_total = n.get("mem_total_mb") or 0
        mem_avail = n.get("mem_available_mb") or 0
        mem_pct = _pct(mem_total - mem_avail, mem_total)
        load = n.get("load1_per_cpu", n.get("load1", 0))
        workers = n.get("hermes_workers", 0)
        cap = n.get("fleet_cap", "?")
        disk = n.get("disk_free_gb", "?")
        done = tput.get(n.get("node", ""), 0)
        rows.append(
            "<tr>"
            f"<td><b>{node}</b><br><small>{html.escape(str(n.get('role','')))}</small></td>"
            f"<td>{''.join(badges)}</td>"
            f"<td class='num'>{load:.2f}</td>"
            f"<td class='num'>{mem_avail:.0f}/{mem_total:.0f} MB <small>({mem_pct:.0f}%)</small></td>"
            f"<td class='num'>{disk}</td>"
            f"<td class='num'>{workers}/{cap}</td>"
            f"<td class='num'>{done}</td>"
            f"<td class='num'>{n.get('units_1h','-')}</td>"
            "</tr>"
        )
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Hermes fleet dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{{background:#0f1216;color:#e6e6e6;font:14px/1.5 system-ui,monospace;margin:0;padding:24px}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a5;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%}} th,td{{padding:8px 10px;border-bottom:1px solid #232a31;text-align:left}}
 th{{color:#8b98a5;font-weight:600}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .badge{{display:inline-block;padding:1px 6px;border-radius:4px;margin-right:3px;font-size:11px}}
 .ok{{background:#15351f;color:#59d98a}} .warn{{background:#3a3211;color:#e6c34a}}
 .bad{{background:#3a1414;color:#ff7b72}} .na{{background:#20262c;color:#8b98a5}}
 small{{color:#8b98a5}}
</style></head><body>
<h1>Hermes fleet dashboard</h1>
<div class="sub">{len(nodes)} nodes · generated {ts} · badges: gateway/workers/kanban/responder/fips/timers (green=ok, amber=degraded, red=down)</div>
<table><thead><tr>
<th>Node</th><th>Components</th><th>Load/core</th><th>Mem avail</th><th>Disk free (GB)</th><th>Workers</th><th>Tasks&#8203;/24h</th><th>Units/1h</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Hermes fleet dashboard builder")
    ap.add_argument("--bot", default=str(BOT))
    ap.add_argument("--boards", default=str(BOARDS))
    ap.add_argument("--out", default=str(Path.home() / "nsites" / "fleet-dashboard" / "index.html"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    nodes = collect(Path(args.bot))
    tput = throughput(Path(args.boards))
    payload = {"ts": time.time(), "nodes": nodes, "throughput_24h": tput}
    if args.json:
        print(json.dumps(payload, indent=1))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(nodes, tput))
    print(f"wrote {out} ({len(nodes)} nodes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
