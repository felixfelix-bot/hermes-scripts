#!/usr/bin/env python3
"""Unit tests for rate_limit_gate_drift_guard.py (kanban t_683a897b).

The guard protects manager-deployed scripts in ~/.hermes/bot from being
silently reverted by foreign bot-dir syncs (incident: 2026-08-15 20:14 IST
opencode sync clobbered rate_limit_gate.py to the pre-T3.1 version).

Synthetic tmp git repos only — no live ~/.hermes paths touched.

Run:
    python3 -m pytest tests/test_rate_limit_gate_drift_guard.py -q
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rate_limit_gate_drift_guard import DriftGuard

GOOD_V5 = "#!/usr/bin/env python3\n# full 5-condition gate (master)\nSTATE = 'recent_503+quota_windows'\n".encode()
OLD_V3 = "#!/usr/bin/env python3\n# old 3-condition gate\nSTATE = 'legacy'\n".encode()
BAD_SYNTAX = b"def broken(:\n    pass\n"


def make_repo(tmpdir, relpath, blob):
    """Create a git repo at tmpdir/repo with `relpath` committed on master."""
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    target = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(blob)
    subprocess.run(["git", "add", relpath], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    # origin/master ref so `git show origin/master:<rel>` works without a remote
    subprocess.run(["git", "update-ref", "refs/remotes/origin/master",
                    subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                   capture_output=True, text=True, check=True).stdout.strip()],
                   cwd=repo, check=True)
    return repo


class DriftGuardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="driftguard-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp, "rate_limit_gate.py", GOOD_V5)
        self.home = os.path.join(self.tmp, "guardhome")
        self.deployed = os.path.join(self.tmp, "bot", "rate_limit_gate.py")
        os.makedirs(os.path.dirname(self.deployed), exist_ok=True)
        self.guarded = [(self.deployed, "rate_limit_gate.py")]

    def write_deployed(self, blob, mode=0o664):
        with open(self.deployed, "wb") as fh:
            fh.write(blob)
        os.chmod(self.deployed, mode)

    def guard(self, cooldown=21600, now=None):
        return DriftGuard(repo_dir=self.repo, guarded=self.guarded,
                          home=self.home, cooldown_s=cooldown, now=now)


class TestHealthy(DriftGuardTestBase):
    def test_healthy_run_is_silent(self):
        self.write_deployed(GOOD_V5)
        code, alert = self.guard().run()
        self.assertEqual(code, 0)
        self.assertEqual(alert, "")


class TestDrift(DriftGuardTestBase):
    def test_drift_heals_alerts_and_preserves_forensic(self):
        self.write_deployed(OLD_V3)
        code, alert = self.guard().run()
        self.assertEqual(code, 0)
        self.assertIn("rate_limit_gate.py", alert)
        self.assertIn("healed", alert.lower())
        # deployed now matches master
        with open(self.deployed, "rb") as fh:
            self.assertEqual(fh.read(), GOOD_V5)
        # forensic copy of the clobbered bytes exists
        forensic_dir = os.path.join(self.home, "forensic")
        copies = [f for f in os.listdir(forensic_dir)] if os.path.isdir(forensic_dir) else []
        self.assertTrue(copies, "forensic copy of drifted file must be preserved")
        with open(os.path.join(forensic_dir, copies[0]), "rb") as fh:
            self.assertEqual(fh.read(), OLD_V3)

    def test_heal_preserves_file_mode(self):
        self.write_deployed(OLD_V3, mode=0o600)
        self.guard().run()
        self.assertEqual(os.stat(self.deployed).st_mode & 0o777, 0o600)

    def test_check_only_reports_but_does_not_touch(self):
        self.write_deployed(OLD_V3)
        code, alert = self.guard().run(check_only=True)
        self.assertEqual(code, 0)
        self.assertIn("drift", alert.lower())
        with open(self.deployed, "rb") as fh:
            self.assertEqual(fh.read(), OLD_V3, "check-only must NOT modify deployed file")

    def test_cooldown_suppresses_repeat_alert_but_still_heals(self):
        self.write_deployed(OLD_V3)
        # pre-seed state: drift alert fired 60s ago
        os.makedirs(self.home, exist_ok=True)
        now = 1_800_000_000
        with open(os.path.join(self.home, "state.json"), "w") as fh:
            json.dump({"alerts": {"drift:rate_limit_gate.py": now - 60}}, fh)
        self.write_deployed(OLD_V3)  # re-clobbered after earlier heal
        code, alert = self.guard(now=now).run()
        self.assertEqual(code, 0)
        self.assertEqual(alert, "", "within cooldown the alert must be suppressed")
        with open(self.deployed, "rb") as fh:
            self.assertEqual(fh.read(), GOOD_V5, "heal must still happen inside cooldown")


class TestMissing(DriftGuardTestBase):
    def test_missing_deployed_is_restored(self):
        # do not create the deployed file at all
        code, alert = self.guard().run()
        self.assertEqual(code, 0)
        self.assertIn("restored", alert.lower())
        with open(self.deployed, "rb") as fh:
            self.assertEqual(fh.read(), GOOD_V5)


class TestRepoFailure(DriftGuardTestBase):
    def test_repo_error_alerts_and_never_touches_deployed(self):
        self.write_deployed(OLD_V3)
        guarded = [(self.deployed, "no_such_file_in_repo.py")]
        g = DriftGuard(repo_dir=self.repo, guarded=guarded, home=self.home)
        code, alert = g.run()
        self.assertEqual(code, 0)
        self.assertIn("repo", alert.lower())
        with open(self.deployed, "rb") as fh:
            self.assertEqual(fh.read(), OLD_V3, "repo error must never heal/destroy deployed file")


class TestMasterInvalid(DriftGuardTestBase):
    def test_master_blob_syntax_error_never_deployed(self):
        bad_repo = make_repo(self.tmp, "gate_bad.py", BAD_SYNTAX)
        deployed_bad = os.path.join(self.tmp, "bot2", "gate_bad.py")
        os.makedirs(os.path.dirname(deployed_bad), exist_ok=True)
        with open(deployed_bad, "wb") as fh:
            fh.write(OLD_V3)
        g = DriftGuard(repo_dir=bad_repo,
                       guarded=[(deployed_bad, "gate_bad.py")], home=self.home)
        code, alert = g.run()
        self.assertEqual(code, 0)
        self.assertIn("invalid", alert.lower())
        with open(deployed_bad, "rb") as fh:
            self.assertEqual(fh.read(), OLD_V3, "non-compiling master blob must NOT be deployed")


if __name__ == "__main__":
    unittest.main()
