"""Tests for kanban_mirror_tripwire.py threshold decision + self-test.

Covers:
  (i) evaluate() raises a finding only when a metric is strictly greater than
      its threshold, with the value, threshold and suggested action.
  (ii) The CLI --self-test exercises the parser + decision logic and returns
       PASS/FAIL text on stdout.

No live repo is required; metrics are injected.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import kanban_mirror_tripwire as kt  # noqa: E402


# ── (i) threshold decision ───────────────────────────────────────────────────

def test_evaluate_empty_when_healthy():
    metrics = {"objects": 100, "packs": 1, "commits": 1, "size_mb": 1.0}
    assert kt.evaluate(metrics) == []


def test_evaluate_finds_each_over_threshold():
    metrics = {"objects": 500_000, "packs": 12, "commits": 300, "size_mb": 250.0}
    findings = kt.evaluate(metrics)
    assert len(findings) == 4
    by = {f["metric"]: f for f in findings}
    assert by["objects"]["value"] == 500_000
    assert by["objects"]["threshold"] == kt.DEFAULT_THRESHOLDS["objects"]
    assert by["packs"]["action"] == "run `kanban_git.py compact`"
    assert by["size_mb"]["value"] == 250.0


def test_evaluate_boundary_not_exceeded():
    # Strict > at the boundary.
    metrics = {
        "objects": kt.DEFAULT_THRESHOLDS["objects"],
        "packs": kt.DEFAULT_THRESHOLDS["packs"],
        "commits": kt.DEFAULT_THRESHOLDS["commits"],
        "size_mb": kt.DEFAULT_THRESHOLDS["size_mb"],
    }
    assert kt.evaluate(metrics) == []


def test_evaluate_missing_metrics_silent():
    assert kt.evaluate({}) == []


def test_evaluate_respects_custom_thresholds():
    metrics = {"objects": 50, "packs": 5, "commits": 30, "size_mb": 100.0}
    thresholds = {"objects": 100, "packs": 4, "commits": 25, "size_mb": 99.9}
    findings = kt.evaluate(metrics, thresholds)
    by = {f["metric"]: f for f in findings}
    assert "objects" not in by
    assert "packs" in by
    assert "commits" in by
    assert "size_mb" in by


# ── (ii) self-test integration ───────────────────────────────────────────────

def test_self_test_runs_and_reports_pass():
    # Run the script with --self-test and verify PASS appears and rc==0.
    script = REPO_ROOT / "kanban_mirror_tripwire.py"
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    r = subprocess.run([sys.executable, str(script), "--self-test"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = (r.stdout or "") + (r.stderr or "")
    assert "self-test: PASS" in out, out
    assert "FAIL" not in out.split("self-test:")[0], out


def test_cli_healthy_repo_silent(tmp_path):
    # Build a tiny git repo, run the tripwire, verify empty stdout + rc 0.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    (repo / "file.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True)
    r = subprocess.run([sys.executable, str(REPO_ROOT / "kanban_mirror_tripwire.py"),
                        "--repo", str(repo), "--objects", "1000000", "--packs", "100",
                        "--size-mb", "1000.0", "--commits", "1000"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_cli_alert_printed_when_threshold_exceeded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    (repo / "file.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True)
    r = subprocess.run([sys.executable, str(REPO_ROOT / "kanban_mirror_tripwire.py"),
                        "--repo", str(repo), "--objects", "0", "--packs", "0",
                        "--size-mb", "0.0", "--commits", "0"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "kanban-mirror-tripwire" in r.stdout
    assert "kanban_git.py compact" in r.stdout
