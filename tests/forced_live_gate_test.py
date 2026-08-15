#!/usr/bin/env python3
"""Forced-live gate test (T3.1 verification): fixture DB + fixture quota server.

Scenario A: fixture DB with a z.ai 503 burst (3 server-error anomalies in
10 min) -> gate must write paused=true, reason zai-503-outage, resume_at =
now + min(Retry-After, 20 min); exit code 1.

Scenario B: no burst, but fixture /quota with an 88%-used 5-hour window ->
paused=true with resume_at = window reset; exit code 1.

Scenario C: clean fixture -> paused=false, exit code 0.

The staggered-dispatch check_gate() only reads "paused", which stays the
top-level key — verified here by feeding the written state file through the
same json logic the dispatcher uses.

Run from repo root:  python3 tests/forced_live_gate_test.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(REPO, "rate_limit_gate.py")


def make_fixture_db(path, burst):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE anomaly_events (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, severity TEXT,
             category TEXT, title TEXT, detail TEXT)""")
    conn.execute(
        """CREATE TABLE api_calls (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key_name TEXT,
             key_suffix TEXT, model TEXT, prompt_tokens INTEGER,
             completion_tokens INTEGER, total_tokens INTEGER, tier TEXT,
             cache_hit INTEGER, ollama_hit INTEGER, ppq_hit INTEGER,
             status_code INTEGER, error TEXT, duration_ms INTEGER,
             cost_usd REAL, cost_source TEXT)""")
    conn.execute(
        """CREATE TABLE rate_limit_samples (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
             inter_arrival REAL, consecutive INTEGER, wait_used REAL,
             source TEXT)""")
    conn.execute(
        """CREATE TABLE kalman_samples (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT,
             window TEXT, used_pct_observed REAL,
             projected_additional_pct REAL, projected_total_pct REAL,
             burn_rate_tph REAL, velocity_tph2 REAL, uncertainty REAL,
             exhausts_in_hours REAL, will_exhaust INTEGER, note TEXT)""")
    now = time.time()
    for i in range(burst):
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail)"
            " VALUES (?,?,?,?,?)",
            (now - 60 * (i + 1), "WARN", "key_backoff",
             f"ours server failure #{i + 1}",
             "backoff 30s; error_type=server"))
    conn.commit()
    conn.close()


class QuotaHandler(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def run_gate(db_path, state_path, quota_url):
    env = dict(os.environ)
    env["HERMES_GATE_STATE"] = state_path
    env["HERMES_GATE_QUOTA_URL"] = quota_url
    proc = subprocess.run(
        [sys.executable, GATE, "--db", db_path],
        capture_output=True, text=True, env=env, timeout=60)
    with open(state_path) as f:
        return proc.returncode, json.load(f)


def dispatcher_view_blocked(state):
    """Mirror of staggered-dispatch.sh check_gate JSON logic."""
    return bool(state.get("paused")) or bool(state.get("blocked")) \
        or bool(state.get("tripped")) \
        or state.get("dispatch_allowed") is False


def main():
    tmp = tempfile.mkdtemp(prefix="gate-live-test-", dir=REPO)
    reset_at = time.time() + 3600
    quota_server = HTTPServer(("127.0.0.1", 0), QuotaHandler)
    port = quota_server.server_address[1]
    threading.Thread(target=quota_server.serve_forever, daemon=True).start()
    quota_url = f"http://127.0.0.1:{port}/quota"

    failures = []

    # Scenario A: 503 burst (quota payload healthy, 5% used)
    QuotaHandler.payload = {"ours": {"windows": [
        {"name": "5-hour", "used_pct": 5, "resets_at": reset_at,
         "window_hours": 5}], "age_s": 10}}
    db_a = os.path.join(tmp, "burst.db")
    make_fixture_db(db_a, burst=3)
    state_a = os.path.join(tmp, "state_a.json")
    rc, state = run_gate(db_a, state_a, quota_url)
    before = time.time()
    if rc != 1 or not state["paused"]:
        failures.append(f"A: expected paused exit 1, got rc={rc} paused={state['paused']}")
    if "zai-503-outage" not in state["reason"]:
        failures.append(f"A: reason missing zai-503-outage: {state['reason']!r}")
    resume_ts = datetime.fromisoformat(state["resume_at"]).timestamp() \
        if state["resume_at"] else 0
    # 30s proxy backoff hint is the min(Retry-After, 20min) result
    if not (before + 25 <= resume_ts <= time.time() + 1200 + 5):
        failures.append(f"A: resume_at implausible: {state['resume_at']}")
    if not dispatcher_view_blocked(state):
        failures.append("A: staggered-dispatch view would NOT block")

    # Scenario A2: burst of only 2 -> clear (threshold is >=3)
    db_a2 = os.path.join(tmp, "burst2.db")
    make_fixture_db(db_a2, burst=2)
    state_a2 = os.path.join(tmp, "state_a2.json")
    rc, state = run_gate(db_a2, state_a2, quota_url)
    if rc != 0 or state["paused"]:
        failures.append(f"A2: expected clear, got rc={rc} paused={state['paused']} "
                        f"reason={state['reason']!r}")

    # Scenario B: no burst, hot quota window (88%)
    QuotaHandler.payload = {"ours": {"windows": [
        {"name": "5-hour", "used_pct": 88, "resets_at": reset_at,
         "window_hours": 5}], "age_s": 10}}
    db_b = os.path.join(tmp, "clean.db")
    make_fixture_db(db_b, burst=0)
    state_b = os.path.join(tmp, "state_b.json")
    rc, state = run_gate(db_b, state_b, quota_url)
    if rc != 1 or not state["paused"]:
        failures.append(f"B: expected paused, got rc={rc} reason={state['reason']!r}")
    if "QUOTA-WINDOW" not in (state["reason"] or ""):
        failures.append(f"B: reason missing QUOTA-WINDOW: {state['reason']!r}")
    resume_ts = datetime.fromisoformat(state["resume_at"]).timestamp() \
        if state["resume_at"] else 0
    if abs(resume_ts - reset_at) > 5:
        failures.append(f"B: resume_at != window reset: {state['resume_at']}")
    if not dispatcher_view_blocked(state):
        failures.append("B: staggered-dispatch view would NOT block")

    # Scenario C: clean DB + healthy quota -> clear
    QuotaHandler.payload = {"ours": {"windows": [
        {"name": "5-hour", "used_pct": 5, "resets_at": reset_at,
         "window_hours": 5}], "age_s": 10}}
    db_c = os.path.join(tmp, "clean2.db")
    make_fixture_db(db_c, burst=0)
    state_c = os.path.join(tmp, "state_c.json")
    rc, state = run_gate(db_c, state_c, quota_url)
    if rc != 0 or state["paused"]:
        failures.append(f"C: expected clear, got rc={rc} reason={state['reason']!r}")
    if "ts" not in state or "checked_at" not in state:
        failures.append("C: state file missing ts/checked_at keys")

    # Scenario D: quota collector unreachable -> fail-open (clear, no burst)
    db_d = os.path.join(tmp, "clean3.db")
    make_fixture_db(db_d, burst=0)
    state_d = os.path.join(tmp, "state_d.json")
    rc, state = run_gate(db_d, state_d, "http://127.0.0.1:1/quota")
    if rc != 0 or state["paused"]:
        failures.append(f"D: expected fail-open clear, got rc={rc} "
                        f"reason={state['reason']!r}")
    if not state["checks"]["quota_windows"].get("fail_open"):
        failures.append("D: quota_windows check missing fail_open flag")

    quota_server.shutdown()
    for name in os.listdir(tmp):
        os.unlink(os.path.join(tmp, name))
    os.rmdir(tmp)

    if failures:
        print("FORCED-LIVE FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("forced-live gate test: ALL SCENARIOS PASS (A burst-pause, A2 threshold, "
          "B quota-pause, C clear, D fail-open)")


if __name__ == "__main__":
    main()
