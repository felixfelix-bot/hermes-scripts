#!/usr/bin/env python3
"""Unit tests for rate_limit_gate.py — T3.1 recent_503 + quota_windows checks.

Synthetic sample sets only; no live DB or network required. The decision
helpers are pure functions taking explicit ``now`` so behaviour is
deterministic.

Run:
    python3 -m unittest discover -s tests -v
    python3 -m pytest tests/ -q   (if pytest available)
"""

import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rate_limit_gate as gate


NOW = 1_800_000_000.0  # fixed reference time for all synthetic sets


def ev(ts_offset_s, retry_after_s=None, source="anomaly"):
    return {
        "ts": NOW + ts_offset_s,
        "source": source,
        "retry_after_s": retry_after_s,
    }


def win(key="ours", name="5-hour", used_pct=50.0, resets_at=NOW + 3600):
    return {"key": key, "name": name, "used_pct": used_pct, "resets_at": resets_at}


class TestEvaluateRecent503(unittest.TestCase):
    def test_burst_of_three_triggers(self):
        r = gate.evaluate_recent_503([ev(-60), ev(-120), ev(-300)], now=NOW)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["count"], 3)
        self.assertIn("zai-503-outage", r["reason"])
        self.assertEqual(r["resume_offset"], gate.MAX_503_RESUME_S)

    def test_two_events_do_not_trigger(self):
        r = gate.evaluate_recent_503([ev(-60), ev(-120)], now=NOW)
        self.assertFalse(r["triggered"])
        self.assertIsNone(r["reason"])

    def test_old_events_outside_window_ignored(self):
        events = [ev(-60), ev(-120), ev(-601)]  # third one > 10 min old
        r = gate.evaluate_recent_503(events, now=NOW)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["count"], 2)

    def test_boundary_exactly_600s_old_counts(self):
        r = gate.evaluate_recent_503([ev(-600), ev(-60), ev(-120)], now=NOW)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["count"], 3)

    def test_empty_events_fail_open(self):
        r = gate.evaluate_recent_503([], now=NOW)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["fail_open"])

    def test_retry_after_capped_at_20min(self):
        r = gate.evaluate_recent_503(
            [ev(-60, retry_after_s=7200), ev(-120, retry_after_s=7200),
             ev(-300, retry_after_s=7200)],
            now=NOW,
        )
        self.assertTrue(r["triggered"])
        self.assertEqual(r["resume_offset"], gate.MAX_503_RESUME_S)  # 1200

    def test_small_retry_after_honoured(self):
        r = gate.evaluate_recent_503(
            [ev(-60, retry_after_s=300), ev(-120), ev(-300)],
            now=NOW,
        )
        self.assertTrue(r["triggered"])
        self.assertEqual(r["resume_offset"], 300)

    def test_max_retry_hint_across_events_used(self):
        r = gate.evaluate_recent_503(
            [ev(-60, retry_after_s=45), ev(-120, retry_after_s=600), ev(-300)],
            now=NOW,
        )
        self.assertEqual(r["resume_offset"], 600)


class TestPick503Decision(unittest.TestCase):
    def test_triggered_source_with_most_events_wins(self):
        results = [
            {"triggered": False, "count": 0, "source": "anomaly"},
            {"triggered": True, "count": 4, "source": "journal",
             "resume_offset": 1200, "reason": "x"},
        ]
        r = gate.pick_503_decision(results)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["count"], 4)

    def test_all_clear_returns_clear(self):
        results = [
            {"triggered": False, "count": 0, "source": "anomaly"},
            {"triggered": False, "count": 1, "source": "api_calls"},
        ]
        r = gate.pick_503_decision(results)
        self.assertFalse(r["triggered"])

    def test_empty_results(self):
        r = gate.pick_503_decision([])
        self.assertFalse(r["triggered"])
        self.assertTrue(r["fail_open"])


class TestEvaluateQuotaWindows(unittest.TestCase):
    def test_window_at_85_triggers_with_reset_resume(self):
        r = gate.evaluate_quota_windows(
            [win(used_pct=85.0, resets_at=NOW + 7200)], now=NOW
        )
        self.assertTrue(r["triggered"])
        self.assertEqual(r["window"], "5-hour")
        self.assertEqual(r["resume_at_ts"], NOW + 7200)

    def test_window_below_85_does_not_trigger(self):
        r = gate.evaluate_quota_windows(
            [win(used_pct=84.9, resets_at=NOW + 7200)], now=NOW
        )
        self.assertFalse(r["triggered"])

    def test_worst_window_selected(self):
        windows = [
            win(name="5-hour", used_pct=86.0, resets_at=NOW + 600),
            win(name="monthly", used_pct=92.5, resets_at=NOW + 86400),
        ]
        r = gate.evaluate_quota_windows(windows, now=NOW)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["window"], "monthly")
        self.assertEqual(r["used_pct"], 92.5)
        self.assertEqual(r["resume_at_ts"], NOW + 86400)

    def test_missing_resets_at_uses_fallback_offset(self):
        r = gate.evaluate_quota_windows(
            [win(used_pct=90.0, resets_at=None)], now=NOW
        )
        self.assertTrue(r["triggered"])
        self.assertEqual(r["resume_at_ts"], NOW + gate.QUOTA_FALLBACK_RESUME_S)
        self.assertTrue(r.get("fallback_resume"))

    def test_already_reset_window_ignored(self):
        r = gate.evaluate_quota_windows(
            [win(used_pct=95.0, resets_at=NOW - 60)], now=NOW
        )
        self.assertFalse(r["triggered"])

    def test_empty_windows_fail_open(self):
        r = gate.evaluate_quota_windows([], now=NOW)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["fail_open"])

    def test_reason_names_window_and_key(self):
        r = gate.evaluate_quota_windows(
            [win(key="friend", name="weekly", used_pct=88.0,
                 resets_at=NOW + 3600)],
            now=NOW,
        )
        self.assertIn("friend", r["reason"])
        self.assertIn("weekly", r["reason"])
        self.assertIn("85", r["reason"])


class TestCollect503Events(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE anomaly_events (
                 id INTEGER PRIMARY KEY, ts REAL, severity TEXT,
                 category TEXT, title TEXT, detail TEXT)"""
        )
        conn.execute(
            """CREATE TABLE api_calls (
                 id INTEGER PRIMARY KEY, ts REAL, key_name TEXT,
                 key_suffix TEXT, model TEXT, prompt_tokens INTEGER,
                 completion_tokens INTEGER, total_tokens INTEGER, tier TEXT,
                 cache_hit INTEGER, ollama_hit INTEGER, ppq_hit INTEGER,
                 status_code INTEGER, error TEXT, duration_ms INTEGER,
                 cost_usd REAL, cost_source TEXT)"""
        )
        return conn

    def test_server_anomalies_collected_with_retry_hint(self):
        conn = self._conn()
        for i, off in enumerate((-30, -90, -150)):
            conn.execute(
                "INSERT INTO anomaly_events (ts, severity, category, title, detail)"
                " VALUES (?,?,?,?,?)",
                (NOW + off, "WARN", "key_backoff",
                 f"ours server failure #{i+1}",
                 "backoff 30s; error_type=server"),
            )
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail)"
            " VALUES (?,?,?,?,?)",
            (NOW - 45, "WARN", "key_backoff", "ours exhausted failure #1",
             "backoff 8s; error_type=exhausted"),
        )
        events = gate.collect_503_events_anomaly(conn, now=NOW)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["retry_after_s"] == 30 for e in events))

    def test_api_calls_503_collected(self):
        conn = self._conn()
        for off in (-20, -80, -140, -200, -700):  # last one outside window
            conn.execute(
                "INSERT INTO api_calls (ts, status_code, error)"
                " VALUES (?,?,?)", (NOW + off, 503, "upstream capacity"))
        events = gate.collect_503_events_api(conn, now=NOW)
        self.assertEqual(len(events), 4)  # window clamp drops the -700 one
        self.assertEqual(events[0]["source"], "api_calls")

    def test_api_calls_timeout_502_and_504_collected(self):
        # Real-world outage class (2026-08-15 evidence, t_8e2673cd): z.ai read
        # timeouts surface ONLY as tier='zai' status_code=502 rows in api_calls
        # ('proxy error: The read operation timed out') — no anomaly rows.
        # The api_calls collector must count 502 and 504 alongside 503.
        conn = self._conn()
        rows = [(-20, 502), (-80, 502), (-140, 504)]
        for off, code in rows:
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, tier, status_code, error)"
                " VALUES (?,?,?,?,?)",
                (NOW + off, "friend", "zai", code,
                 "proxy error: The read operation timed out"))
        events = gate.collect_503_events_api(conn, now=NOW)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["source"], "api_calls")

    def test_api_calls_external_tier_5xx_ignored(self):
        # Failover paths log their own per-provider attempts with
        # tier=<provider_name>; a failing external provider while z.ai is
        # healthy is NOT a z.ai outage and must not trip zai-503-outage.
        conn = self._conn()
        for off in (-20, -80, -140):
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, tier, status_code, error)"
                " VALUES (?,?,?,?,?)",
                (NOW + off, "friend", "deepinfra", 503, "upstream capacity"))
        events = gate.collect_503_events_api(conn, now=NOW)
        self.assertEqual(events, [])

    def test_real_proxy_anomaly_format_collected(self):
        # Byte-realistic fixture: zai_proxy._log_anomaly JSON-wraps the
        # payload as {"detail": ..., "key_name": ...} into anomaly_events.
        conn = self._conn()
        for i, off in enumerate((-30, -90, -150)):
            conn.execute(
                "INSERT INTO anomaly_events (ts, severity, category, title,"
                " detail) VALUES (?,?,?,?,?)",
                (NOW + off, "WARN", "key_backoff",
                 f"ours server failure #{i+1}",
                 json.dumps({"detail": "backoff 30s; error_type=server",
                             "key_name": "ours"})))
        events = gate.collect_503_events_anomaly(conn, now=NOW)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["retry_after_s"] == 30 for e in events))

    def test_real_proxy_anomaly_float_backoff_hint(self):
        # If _SERVER_ERROR_BACKOFF_SECONDS ever becomes a computed float the
        # detail string is 'backoff 12.5s; error_type=server' — the hint
        # parser must stay float-safe inside the JSON-wrapped detail.
        conn = self._conn()
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title,"
            " detail) VALUES (?,?,?,?,?)",
            (NOW - 30, "WARN", "key_backoff", "ours server failure #1",
             json.dumps({"detail": "backoff 12.5s; error_type=server",
                         "key_name": "ours"})))
        events = gate.collect_503_events_anomaly(conn, now=NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["retry_after_s"], 12.5)

    def test_missing_tables_fail_open(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        events = gate.collect_503_events_anomaly(conn, now=NOW)
        self.assertEqual(events, [])
        events = gate.collect_503_events_api(conn, now=NOW)
        self.assertEqual(events, [])


class TestParseQuotaPayload(unittest.TestCase):
    def test_windows_parsed_for_all_keys(self):
        payload = {
            "ours": {"windows": [
                {"name": "monthly", "used_pct": 3, "resets_at": NOW + 86400,
                 "window_hours": 720},
                {"name": "5-hour", "used_pct": 88, "resets_at": NOW + 600,
                 "window_hours": 5},
            ], "age_s": 30},
            "friend": {"windows": [
                {"name": "weekly", "used_pct": 60, "resets_at": NOW + 3600,
                 "window_hours": 168},
            ], "age_s": 30},
            "active": "ours",
        }
        windows = gate.parse_quota_payload(payload, now=NOW)
        self.assertEqual(len(windows), 3)
        by = {(w["key"], w["name"]): w for w in windows}
        self.assertEqual(by[("ours", "5-hour")]["used_pct"], 88)
        self.assertEqual(by[("friend", "weekly")]["used_pct"], 60)

    def test_stale_key_skipped(self):
        payload = {
            "ours": {"windows": [
                {"name": "5-hour", "used_pct": 95, "resets_at": NOW + 600,
                 "window_hours": 5},
            ], "age_s": gate.QUOTA_STALE_S + 60},
        }
        windows = gate.parse_quota_payload(payload, now=NOW)
        self.assertEqual(windows, [])

    def test_malformed_payload_fail_open(self):
        self.assertEqual(gate.parse_quota_payload({}, now=NOW), [])
        self.assertEqual(gate.parse_quota_payload(None, now=NOW), [])

    def test_non_dict_key_section_ignored(self):
        payload = {"active": "ours", "ollama_cloud": {"used_pct": 100.0}}
        windows = gate.parse_quota_payload(payload, now=NOW)
        self.assertEqual(windows, [])


class TestDecisionPriority(unittest.TestCase):
    def test_503_beats_429_and_quota(self):
        r503 = {"triggered": True, "resume_offset": 1200,
                "reason": "zai-503-outage: 3 upstream 503/5xx in last 600s",
                "count": 3}
        r429 = {"triggered": True, "resume_offset": 60, "reason": "ACTIVE 429"}
        quota = {"triggered": True, "resume_at_ts": NOW + 3600,
                 "reason": "QUOTA-WINDOW", "window": "5-hour"}
        paused, reason, resume_at = gate.decide(
            now=NOW, recent_503=r503, recent_429=r429, quota=quota,
            kalman={"triggered": False}, peak={"is_peak": True})
        self.assertTrue(paused)
        self.assertIn("zai-503-outage", reason)
        # T3.2/T3.3 match on a top-level 'zai-*' reason prefix
        self.assertTrue(reason.startswith("zai-"))
        self.assertEqual(resume_at, gate.iso(NOW + 1200))

    def test_quota_beats_kalman(self):
        quota = {"triggered": True, "resume_at_ts": NOW + 7200,
                 "reason": "QUOTA-WINDOW: ours 5-hour at 90%", "window": "5-hour"}
        paused, reason, resume_at = gate.decide(
            now=NOW, recent_503={"triggered": False},
            recent_429={"triggered": False}, quota=quota,
            kalman={"triggered": True, "resume_offset": 600, "reason": "K"},
            peak={"is_peak": False})
        self.assertTrue(paused)
        self.assertIn("QUOTA-WINDOW", reason)
        self.assertEqual(resume_at, gate.iso(NOW + 7200))

    def test_all_clear_is_clear(self):
        paused, reason, resume_at = gate.decide(
            now=NOW, recent_503={"triggered": False},
            recent_429={"triggered": False},
            quota={"triggered": False}, kalman={"triggered": False},
            peak={"is_peak": False})
        self.assertFalse(paused)
        self.assertEqual(reason, "clear")
        self.assertIsNone(resume_at)


if __name__ == "__main__":
    unittest.main()
