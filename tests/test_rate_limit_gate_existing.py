#!/usr/bin/env python3
"""Unit tests for the pre-existing DB checks + run_gate wiring (T3.1 backfill).

All inputs are synthetic in-memory SQLite DBs or patched collectors — no
live zai_usage.db, no network.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rate_limit_gate as gate


NOW = 1_800_000_000.0


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE rate_limit_samples (id INTEGER PRIMARY KEY, "
                 "ts REAL, inter_arrival REAL, consecutive INTEGER, "
                 "wait_used REAL, source TEXT)")
    conn.execute("CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL, "
                 "key_name TEXT, key_suffix TEXT, model TEXT, "
                 "prompt_tokens INTEGER, completion_tokens INTEGER, "
                 "total_tokens INTEGER, tier TEXT, cache_hit INTEGER, "
                 "ollama_hit INTEGER, ppq_hit INTEGER, status_code INTEGER, "
                 "error TEXT, duration_ms INTEGER, cost_usd REAL, "
                 "cost_source TEXT)")
    conn.execute("CREATE TABLE kalman_samples (id INTEGER PRIMARY KEY, "
                 "ts REAL, key TEXT, window TEXT, used_pct_observed REAL, "
                 "projected_additional_pct REAL, projected_total_pct REAL, "
                 "burn_rate_tph REAL, velocity_tph2 REAL, uncertainty REAL, "
                 "exhausts_in_hours REAL, will_exhaust INTEGER, note TEXT)")
    return conn


class TestPeakHour(unittest.TestCase):
    def test_known_peak_hour_flagged(self):
        conn = make_db()
        with mock.patch.object(gate, "utc_now") as now:
            from datetime import datetime, timezone
            now.return_value = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)
            r = gate.check_peak_hour(conn)
        self.assertTrue(r["is_peak"])  # 03Z in KNOWN_PEAK_HOURS
        self.assertEqual(r["current_hour"], 3)

    def test_quiet_hour_with_no_history_clear(self):
        conn = make_db()
        with mock.patch.object(gate, "utc_now") as now:
            from datetime import datetime, timezone
            now.return_value = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)
            r = gate.check_peak_hour(conn)
        self.assertFalse(r["is_peak"])
        self.assertEqual(r["total_429s"], 0)


class TestRecent429(unittest.TestCase):
    def test_429_in_window_triggers_without_hint_column(self):
        # Real schema has no retry_after_estimate column; the trigger must
        # survive the missing-hint query (pre-existing bug, fixed in T3.1).
        conn = make_db()
        conn.execute("INSERT INTO rate_limit_samples (ts) VALUES (?)",
                     (time.time(),))
        r = gate.check_recent_429(conn)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["resume_offset"], 60)

    def test_old_429_outside_window_clear(self):
        conn = make_db()
        conn.execute("INSERT INTO rate_limit_samples (ts) VALUES (?)",
                     (time.time() - gate.RECENT_429_WINDOW - 60,))
        r = gate.check_recent_429(conn)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["count"], 0)

    def test_api_call_429_triggers(self):
        conn = make_db()
        conn.execute("INSERT INTO api_calls (ts, status_code) VALUES (?, ?)",
                     (time.time(), 429))
        r = gate.check_recent_429(conn)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["api_count"], 1)


class TestKalman(unittest.TestCase):
    def test_imminent_exhaustion_triggers(self):
        conn = make_db()
        conn.execute(
            "INSERT INTO kalman_samples (ts, key, window, used_pct_observed,"
            " projected_additional_pct, projected_total_pct, burn_rate_tph,"
            " velocity_tph2, uncertainty, exhausts_in_hours, will_exhaust,"
            " note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), "ours", "5-hour", 95.0, 9.0, 104.0, 5000.0, 0.0,
             10.0, 0.2, 1, "exhausting"))
        r = gate.check_kalman(conn, 300)
        self.assertTrue(r["triggered"])
        self.assertIn("5-hour", r["reason"])

    def test_no_samples_clear(self):
        r = gate.check_kalman(make_db(), 300)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["windows"], [])


class TestRunGateWiring(unittest.TestCase):
    def _dbfile(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE rate_limit_samples (id INTEGER PRIMARY KEY,"
                     " ts REAL, inter_arrival REAL, consecutive INTEGER,"
                     " wait_used REAL, source TEXT)")
        conn.execute("CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL,"
                     " key_name TEXT, key_suffix TEXT, model TEXT,"
                     " prompt_tokens INTEGER, completion_tokens INTEGER,"
                     " total_tokens INTEGER, tier TEXT, cache_hit INTEGER,"
                     " ollama_hit INTEGER, ppq_hit INTEGER,"
                     " status_code INTEGER, error TEXT, duration_ms INTEGER,"
                     " cost_usd REAL, cost_source TEXT)")
        conn.execute("CREATE TABLE kalman_samples (id INTEGER PRIMARY KEY,"
                     " ts REAL, key TEXT, window TEXT, used_pct_observed REAL,"
                     " projected_additional_pct REAL, projected_total_pct REAL,"
                     " burn_rate_tph REAL, velocity_tph2 REAL, uncertainty REAL,"
                     " exhausts_in_hours REAL, will_exhaust INTEGER, note TEXT)")
        conn.execute("CREATE TABLE anomaly_events (id INTEGER PRIMARY KEY,"
                     " ts REAL, severity TEXT, category TEXT, title TEXT,"
                     " detail TEXT)")
        conn.commit()
        conn.close()
        return path

    def test_clear_path_full_shape(self):
        from datetime import datetime, timezone
        healthy = {"ours": {"windows": [
            {"name": "5-hour", "used_pct": 10, "resets_at": NOW + 600,
             "window_hours": 5}], "age_s": 5}}
        quiet = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)  # 18Z: no peak
        with mock.patch.object(gate, "utc_now", return_value=quiet), \
             mock.patch.object(gate, "collect_503_events_journal",
                               return_value=[]), \
             mock.patch.object(gate, "fetch_quota_payload",
                               return_value=healthy):
            result = gate.run_gate(db_path=self._dbfile())
        self.assertFalse(result["paused"])
        self.assertEqual(result["reason"], "clear")
        self.assertIn("recent_503", result["checks"])
        self.assertIn("quota_windows", result["checks"])
        self.assertIn("checked_at", result)

    def test_burst_in_db_pauses_via_run_gate(self):
        import time as _t
        dbfile = self._dbfile()
        conn = sqlite3.connect(dbfile)
        for i in range(3):
            conn.execute(
                "INSERT INTO anomaly_events (ts, severity, category, title,"
                " detail) VALUES (?,?,?,?,?)",
                (_t.time() - 30 * (i + 1), "WARN", "key_backoff",
                 "ours server failure #1", "backoff 30s; error_type=server"))
        conn.commit()
        conn.close()
        healthy = {"ours": {"windows": [
            {"name": "5-hour", "used_pct": 10, "resets_at": NOW + 600,
             "window_hours": 5}], "age_s": 5}}
        with mock.patch.object(gate, "collect_503_events_journal",
                               return_value=[]), \
             mock.patch.object(gate, "fetch_quota_payload",
                               return_value=healthy):
            result = gate.run_gate(db_path=dbfile)
        self.assertTrue(result["paused"])
        self.assertIn("zai-503-outage", result["reason"])
        self.assertEqual(result["checks"]["recent_503"]["source"], "anomaly")

    def test_missing_db_still_evaluates_quota(self):
        hot = {"ours": {"windows": [
            {"name": "5-hour", "used_pct": 90, "resets_at": NOW + 600,
             "window_hours": 5}], "age_s": 5}}
        with mock.patch.object(gate, "collect_503_events_journal",
                               return_value=[]), \
             mock.patch.object(gate, "fetch_quota_payload", return_value=hot):
            result = gate.run_gate(db_path="/nonexistent/zai_usage.db")
        self.assertTrue(result["paused"])
        self.assertIn("QUOTA-WINDOW", result["reason"])


class TestCollectorErrorPaths(unittest.TestCase):
    def test_journal_subprocess_raises_fail_open(self):
        with mock.patch.object(gate.subprocess, "run",
                               side_effect=OSError("no journalctl")):
            self.assertEqual(gate.collect_503_events_journal(NOW), [])

    def test_journal_nonzero_rc_fail_open(self):
        proc = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(gate.subprocess, "run", return_value=proc):
            self.assertEqual(gate.collect_503_events_journal(NOW), [])

    def test_journal_lines_parsed(self):
        lines = ("1780000000.0 h p[1]: upstream 503 error\n"
                 "1780000001.0 h p[1]: 200 OK\n"
                 "1780000002.0 h p[503]: request completed\n")
        proc = mock.Mock(returncode=0, stdout=lines)
        with mock.patch.object(gate.subprocess, "run", return_value=proc):
            events = gate.collect_503_events_journal(now=1780000500.0)
        self.assertEqual(len(events), 1)  # PID-503 line must not count
        self.assertEqual(events[0]["source"], "journal")

    def test_float_backoff_hint_parsed(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE anomaly_events (id INTEGER PRIMARY KEY,"
                     " ts REAL, severity TEXT, category TEXT, title TEXT,"
                     " detail TEXT)")
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail)"
            " VALUES (?,?,?,?,?)",
            (time.time(), "WARN", "key_backoff", "ours server failure #1",
             "backoff 12.5s; error_type=server"))
        events = gate.collect_503_events_anomaly(conn, now=time.time())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["retry_after_s"], 12.5)

    def test_fetch_quota_unreachable_returns_none(self):
        with mock.patch.object(gate.urllib.request, "urlopen",
                               side_effect=OSError("refused")):
            self.assertIsNone(gate.fetch_quota_payload())


class TestWriteState(unittest.TestCase):
    def test_state_written_to_env_override(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.environ["HERMES_GATE_STATE"] = path
        try:
            gate.write_state({"paused": False, "reason": "clear"})
            with open(path) as f:
                self.assertEqual(json.load(f)["reason"], "clear")
        finally:
            del os.environ["HERMES_GATE_STATE"]
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
