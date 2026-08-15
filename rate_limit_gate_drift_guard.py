#!/usr/bin/env python3
"""rate_limit_gate_drift_guard.py — drift watchdog for manager-deployed gate scripts.

Background (kanban t_683a897b, incident 2026-08-15 20:14 IST): a concurrent
opencode session did a bot-dir sync that reverted ~/.hermes/bot/rate_limit_gate.py
to the pre-T3.1 3-condition version, silently downgrading the
``*/5 * * * *`` cron gate so it could no longer see z.ai timeout/502 bursts.

This guard compares the deployed bytes of each guarded file against the
hermes-scripts repo ``origin/master`` blob. On mismatch it:
  1. preserves a forensic copy of the clobbered bytes (timestamped),
  2. verifies the master blob compiles (never deploys a broken blob),
  3. atomically redeploys master HEAD (preserving file mode),
  4. prints ONE alert (cooldown-deduped per violation type).

Cron contract (cron-llm-escalation exception #2 — silent script-only watchdog):
  - exit code is ALWAYS 0 (a non-zero exit would burn LLM quota on error delivery)
  - EMPTY stdout on a healthy run -> scheduler stays silent, zero tokens
  - non-empty stdout -> delivered verbatim to the operator
  - per-violation-type cooldown (default 6h) prevents alert spam while a
    clobbering process keeps re-reverting the file

If a deploy was INTENTIONAL (newer than master), commit it to hermes-scripts
master first; otherwise this guard will keep reverting it every 5 minutes.

Usage:
    rate_limit_gate_drift_guard.py              # guard + self-heal + alert
    rate_limit_gate_drift_guard.py --check-only # report drift, touch nothing
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

DEFAULT_REPO_DIR = os.path.expanduser("~/.hermes/scripts")
DEFAULT_HOME_DIR = os.path.expanduser("~/.hermes/drift-guard")
DEFAULT_COOLDOWN_S = 6 * 3600
DEFAULT_GUARDED = [
    (os.path.expanduser("~/.hermes/bot/rate_limit_gate.py"), "rate_limit_gate.py"),
]
TS_FMT = "%Y%m%d-%H%M%S"


class GitError(Exception):
    pass


class DriftGuard:
    def __init__(self, repo_dir, guarded, home, cooldown_s=DEFAULT_COOLDOWN_S, now=None):
        self.repo_dir = repo_dir
        self.guarded = list(guarded)
        self.home = home
        self.cooldown_s = cooldown_s
        self._now = now
        self.forensic_dir = os.path.join(self.home, "forensic")
        self.state_path = os.path.join(self.home, "state.json")

    # ---------------------------------------------------------------- helpers
    def now(self):
        return self._now if self._now is not None else time.time()

    @staticmethod
    def sha256(data):
        return hashlib.sha256(data).hexdigest()

    def expected_bytes(self, repo_relpath):
        """Bytes of repo_relpath at origin/master. Raises GitError."""
        try:
            res = subprocess.run(
                ["git", "-C", self.repo_dir, "show", "origin/master:%s" % repo_relpath],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(str(exc))
        if res.returncode != 0 or not res.stdout:
            raise GitError(res.stderr.decode(errors="replace").strip()[:200])
        return res.stdout

    def _load_state(self):
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"alerts": {}}

    def _save_state(self, state):
        os.makedirs(self.home, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, self.state_path)

    def _cooldown_active(self, vtype):
        return self.now() - self._load_state()["alerts"].get(vtype, 0) < self.cooldown_s

    def _mark_alerted(self, vtype):
        state = self._load_state()
        state["alerts"][vtype] = self.now()
        self._save_state(state)

    def _forensic_copy(self, deployed_path, data):
        os.makedirs(self.forensic_dir, exist_ok=True)
        base = os.path.basename(deployed_path)
        stamp = time.strftime(TS_FMT, time.localtime(self.now()))
        path = os.path.join(self.forensic_dir, "%s.drift-%s" % (base, stamp))
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.forensic_dir,
                                "%s.drift-%s.%d" % (base, stamp, n))
            n += 1
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _deploy(self, deployed_path, blob):
        """Atomic replace, preserving the existing file mode."""
        mode = 0o644
        if os.path.exists(deployed_path):
            mode = os.stat(deployed_path).st_mode & 0o777
        os.makedirs(os.path.dirname(deployed_path), exist_ok=True)
        tmp = deployed_path + ".driftguard-tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.chmod(tmp, mode)
        os.replace(tmp, deployed_path)

    def _read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    # ------------------------------------------------------------------- main
    def run(self, check_only=False):
        """Returns (exit_code, alert_text). exit_code is always 0."""
        lines = []

        def emit(vtype, text):
            if self._cooldown_active(vtype):
                return
            self._mark_alerted(vtype)
            lines.append(text)

        for deployed_path, repo_relpath in self.guarded:
            name = repo_relpath
            try:
                blob = self.expected_bytes(repo_relpath)
            except GitError as exc:
                emit("repo-error:%s" % name,
                     "DRIFT-GUARD repo-error for %s: cannot read origin/master blob "
                     "from %s (%s). Deployed file left untouched — investigate the "
                     "hermes-scripts repo." % (name, self.repo_dir, exc))
                continue

            try:
                compile(blob, repo_relpath, "exec")
            except SyntaxError as exc:
                emit("master-invalid:%s" % name,
                     "DRIFT-GUARD master-invalid for %s: origin/master blob does not "
                     "compile (%s). NOT deployed — deployed file left untouched. "
                     "Fix master first." % (name, exc))
                continue

            want_sha = self.sha256(blob)

            if not os.path.exists(deployed_path):
                if check_only:
                    emit("missing:%s" % name,
                         "DRIFT-GUARD missing: %s absent from deploy dir "
                         "(check-only, not restored)." % deployed_path)
                    continue
                self._deploy(deployed_path, blob)
                got = self._read(deployed_path) if os.path.exists(deployed_path) else b""
                ok = self.sha256(got) == want_sha
                emit("missing:%s" % name,
                     "DRIFT-GUARD restored missing %s from origin/master "
                     "(sha256 %s...): %s" % (deployed_path, want_sha[:12],
                                             "verified" if ok else "VERIFY FAILED"))
                continue

            current = self._read(deployed_path)
            got_sha = self.sha256(current)
            if got_sha == want_sha:
                continue  # healthy — silent

            if check_only:
                emit("drift:%s" % name,
                     "DRIFT-GUARD drift (check-only): %s sha256 %s... != master %s... "
                     "(%d bytes deployed vs %d on master). NOT touched."
                     % (deployed_path, got_sha[:12], want_sha[:12],
                        len(current), len(blob)))
                continue

            forensic = self._forensic_copy(deployed_path, current)
            self._deploy(deployed_path, blob)
            healed_sha = self.sha256(self._read(deployed_path))
            emit("drift:%s" % name,
                 "DRIFT-GUARD healed %s: deployed sha256 %s... (%d bytes, likely "
                 "reverted by a foreign bot-dir sync) -> redeployed origin/master "
                 "sha256 %s... (%d bytes, %s). Clobbered bytes preserved at %s. "
                 "If this deploy was INTENTIONAL, commit it to hermes-scripts "
                 "master or it will be reverted again."
                 % (deployed_path, got_sha[:12], len(current), healed_sha[:12],
                    len(blob), "verified" if healed_sha == want_sha else "VERIFY FAILED",
                    forensic))

        return 0, "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Drift watchdog for manager-deployed gate scripts "
                    "(heals foreign bot-dir sync clobbers from origin/master).")
    parser.add_argument("--check-only", action="store_true",
                        help="report drift without modifying deployed files")
    parser.add_argument("--repo", default=DEFAULT_REPO_DIR)
    parser.add_argument("--home", default=DEFAULT_HOME_DIR)
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_S)
    args = parser.parse_args(argv)

    guard = DriftGuard(repo_dir=args.repo, guarded=DEFAULT_GUARDED,
                       home=args.home, cooldown_s=args.cooldown)
    code, alert = guard.run(check_only=args.check_only)
    if alert:
        print(alert)
    return code


if __name__ == "__main__":
    sys.exit(main())
