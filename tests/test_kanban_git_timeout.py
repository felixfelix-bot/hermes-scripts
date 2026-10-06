#!/usr/bin/env python3
"""Regression: kanban_git._git must bound every git invocation.

2026-10-06 x240: an unbounded `git pull --rebase` wedged for >1h holding a
rebase + auto-gc. _git must never hang and must surface a 124 on timeout.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
KG_PATH = Path(os.environ.get("KG_PATH", HERE.parent / "kanban_git.py"))


def _load():
    spec = importlib.util.spec_from_file_location("kanban_git_under_test", KG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_git_timeout_returns_124(monkeypatch, tmp_path):
    kg = _load()

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)

    monkeypatch.setattr(kg.subprocess, "run", boom)
    r = kg._git(tmp_path, "pull", "--rebase", "--autostash", timeout=1)
    assert r.returncode == 124
    assert "timed out" in (r.stderr or "")


def test_git_passes_timeout_to_subprocess(monkeypatch, tmp_path):
    kg = _load()
    seen = {}

    def fake(*a, **k):
        seen.update(k)
        return subprocess.CompletedProcess(a[0], 0, "", "")

    monkeypatch.setattr(kg.subprocess, "run", fake)
    kg._git(tmp_path, "status")
    assert seen.get("timeout") == kg.GIT_TIMEOUT_S
