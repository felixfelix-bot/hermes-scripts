"""Tests for kanban_git.py compact + auto-compact + divergence fallback.

Covers:
  (i)  `compact` on a real throwaway BARE repo reduces commit count to 1 and
       preserves the tree hash exactly (no worktree, no `git checkout`).
  (ii) `should_auto_compact` compacts/does-not-compact at the boundary values
       of the three thresholds.
  (iii) `sync` chooses the divergence fallback (fetch + reset --hard) when
       `pull --rebase` would fail on rewritten history.

Git plumbing (``git init --bare``, ``git commit-tree``, ``git update-ref``) is
real, so these are integration tests; only the repo paths are throwaway tmp
directories — no production repo is touched.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import kanban_git as kg  # noqa: E402


def _git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} rc={r.returncode}: {r.stderr}")
    return r


def _make_bare_with_commits(tmp_path, n_commits=3):
    """Build a bare repo with N commits over a small tree. Returns (repo, tree)."""
    repo = tmp_path / "mirror.git"
    _git("init", "--bare", str(repo))
    # Seed the bare repo via a scratch clone: N commits each touching a file.
    scratch = tmp_path / "scratch"
    _git("clone", str(repo), str(scratch))
    _git("-C", str(scratch), "config", "user.name", "t")
    _git("-C", str(scratch), "config", "user.email", "t@t")
    for i in range(n_commits):
        (scratch / "file.txt").write_text(f"line {i}\n")
        _git("-C", str(scratch), "add", "file.txt")
        _git("-C", str(scratch), "commit", "-q", "-m", f"commit {i}")
    _git("-C", str(scratch), "push", "origin", "master")
    tree = _git("-C", str(scratch), "rev-parse", "master^{tree}").stdout.strip()
    return repo, tree


def _bare_commit_count(repo):
    r = _git("--git-dir", str(repo), "rev-list", "--count", "master")
    return int(r.stdout.strip())


def _bare_tree(repo):
    return _git("--git-dir", str(repo), "rev-parse", "master^{tree}").stdout.strip()


# ── (i) compact on a real bare repo ──────────────────────────────────────────

def test_compact_bare_repo_one_commit_same_tree(tmp_path):
    repo, tree = _make_bare_with_commits(tmp_path, n_commits=5)
    assert _bare_commit_count(repo) == 5

    # compact() must work with NO worktree: it reads HEAD from the bare repo.
    # The branch is "master"; kg.compact resolves it via `git -C <bare> rev-parse`.
    rc = kg.compact(repo)
    assert rc == 0

    assert _bare_commit_count(repo) == 1
    assert _bare_tree(repo) == tree  # tree hash preserved exactly


# ── (ii) auto-compact threshold boundary ──────────────────────────────────────

def test_should_auto_compact_boundaries(tmp_path, monkeypatch):
    # Isolate the three measured signals so each threshold is the only variable.
    monkeypatch.setattr(kg, "_commit_count", lambda repo: 0)
    monkeypatch.setattr(kg, "_pack_count", lambda repo: 0)
    monkeypatch.setattr(kg, "_gitdir_size_mb", lambda repo: 0.0)
    repo = tmp_path / "whatever"  # unused by the stubbed probes
    assert kg.should_auto_compact(repo) is False

    # commits: exactly at 200 -> no, 201 -> yes
    monkeypatch.setattr(kg, "_commit_count", lambda repo: kg.AUTO_COMPACT_COMMITS)
    assert kg.should_auto_compact(repo) is False
    monkeypatch.setattr(kg, "_commit_count", lambda repo: kg.AUTO_COMPACT_COMMITS + 1)
    assert kg.should_auto_compact(repo) is True

    # packs: exactly at 10 -> no, 11 -> yes
    monkeypatch.setattr(kg, "_commit_count", lambda repo: 0)
    monkeypatch.setattr(kg, "_pack_count", lambda repo: kg.AUTO_COMPACT_PACKS)
    assert kg.should_auto_compact(repo) is False
    monkeypatch.setattr(kg, "_pack_count", lambda repo: kg.AUTO_COMPACT_PACKS + 1)
    assert kg.should_auto_compact(repo) is True

    # size: exactly at 200 MB -> no, 200.1 MB -> yes
    monkeypatch.setattr(kg, "_pack_count", lambda repo: 0)
    monkeypatch.setattr(kg, "_gitdir_size_mb", lambda repo: kg.AUTO_COMPACT_SIZE_MB)
    assert kg.should_auto_compact(repo) is False
    monkeypatch.setattr(kg, "_gitdir_size_mb", lambda repo: kg.AUTO_COMPACT_SIZE_MB + 0.1)
    assert kg.should_auto_compact(repo) is True


def test_should_auto_compact_real_repo_under_threshold(tmp_path):
    repo, _tree = _make_bare_with_commits(tmp_path, n_commits=3)
    # A tiny bare repo must never trip the thresholds.
    assert kg.should_auto_compact(repo) is False


# ── (iii) divergence fallback is chosen when rebase would fail ────────────────

def _fake_git_script(tmp_path, pull_rc, script):
    """Drop a fake `git` on PATH that records invocations and fakes pull."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(script)
    (bindir / "git").chmod(0o755)
    return str(bindir)


def test_sync_divergence_fallback_reset(tmp_path, monkeypatch, capsys):
    # A throwaway worktree repo (not bare) for sync(); git calls are stubbed.
    repo = tmp_path / "mirror"
    repo.mkdir()
    _git("init", str(repo))
    _git("-C", str(repo), "config", "user.name", "t")
    _git("-C", str(repo), "config", "user.email", "t@t")
    (repo / "boards").mkdir()
    (repo / "boards" / "b").mkdir()
    (repo / "boards" / "b" / "index.json").write_text("{}")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-q", "-m", "seed")

    boards_root = tmp_path / "boards_root"
    boards_root.mkdir()

    # Stub git: export/materialize need a real repo for the commit path, but the
    # test targets the divergence decision. Make `git pull --rebase` fail with a
    # non-fast-forward, and `git reset --hard` succeed; log argv to a file.
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    log = tmp_path / "git.log"
    script = f'''#!/usr/bin/env python3
import sys
with open({str(log)!r}, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "pull" in args and "--rebase" in args:
    sys.stderr.write("fatal: refusing to merge unrelated histories\\n")
    sys.exit(1)
if "reset" in args and "--hard" in args:
    sys.exit(0)  # divergence fallback: succeed without touching the throwaway repo
import subprocess
r = subprocess.run([{str(real_git)!r}] + args, capture_output=True, text=True)
sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); sys.exit(r.returncode)
'''
    bindir = _fake_git_script(tmp_path, 1, script)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ.get("PATH", ""))

    # Stub the expensive/network pieces so the decision path is deterministic.
    monkeypatch.setattr(kg, "materialize", lambda *a, **k: {})
    monkeypatch.setattr(kg, "export", lambda *a, **k: 0)
    monkeypatch.setattr(kg, "should_auto_compact", lambda repo: False)

    rc = kg.sync(repo, boards_root)
    assert rc == 0

    calls = log.read_text().splitlines()
    joined = "\n".join(calls)
    assert any("reset --hard origin/master" in c for c in calls), \
        "divergence fallback must run `git reset --hard origin/master`"
    # and the fallback was reported on stderr
    err = capsys.readouterr().err
    assert "fell back" in err
