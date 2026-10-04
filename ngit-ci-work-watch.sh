#!/usr/bin/env bash
# ngit-ci-work-watch — reports ONCE when an isolated worker unit finishes.
# Zero-token watchdog (cron no_agent=True): silent unless something new completed.
set -uo pipefail

STATE="$HOME/.hermes/profiles/manager/state/ngit-ci-watch-seen.json"
mkdir -p "$(dirname "$STATE")"
[ -f "$STATE" ] || echo '{}' > "$STATE"

python3 - "$STATE" <<'PY'
import json, os, subprocess, sys

state_path = sys.argv[1]
try:
    seen = json.load(open(state_path))
except Exception:
    seen = {}

W = "/home/c03rad0r/worktrees/t_77a0993c"
# unit -> (label, result_file, log_file)
UNITS = {
    "dash-deploy":      ("dashboard deploy (ci.orangesync.tech)",  f"{W}/dash-summary.md",   f"{W}/dash.log"),
    "ngit-review-r1c":  ("R1c re-review (cycle 2 of 2)",           f"{W}/r1c-verdict.json",  f"{W}/review-r1c.log"),
    "kit-deploy":       ("KIT recon run (legacy, superseded)",     f"{W}/kit-summary.md",    f"{W}/kit.log"),
    "ngit-ci-e2e":      ("ngit-ci end-to-end proof (live trigger)", f"{W}/e2e-ci-proof.md",  f"{W}/e2e.log"),
    "ngit-ci-repos":    ("coordinator NGIT_CI_REPOS switch",       f"{W}/apply-repos.out",   f"{W}/apply-repos.log"),
    "ngit-ci-pilot":    ("ngit-ci migration pilot (3 repos)",      f"{W}/pilot-migration.md", f"{W}/pilot.log"),
    "ngit-ci-bridge":   ("GitHub->ngit CI bridge build",           f"{W}/bridge-report.md",  f"{W}/bridge.log"),
    "ngit-ci-kalman":   ("Kalman-driven CI concurrency + Docker runner", f"{W}/kalman-ci-concurrency.md", f"{W}/ci2.log"),
    "ngit-ci-idleguard": ("CI concurrency idle-guard race fix",       f"{W}/idle-guard-fix.md", f"{W}/idle-guard.log"),
    "ngit-dash-rb":     ("dashboard repo+branch + error surfacing",  f"{W}/dashboard-repo-branch.md", f"{W}/dash-rb.log"),
    "kp-id2":           ("KeePass key identity scan",               f"{W}/kp-id.out",        f"{W}/kp-id.out"),
}

def state(unit):
    return subprocess.run(["systemctl", "--user", "is-active", unit],
                          capture_output=True, text=True).stdout.strip()

out = []
for unit, (label, result, log) in UNITS.items():
    st = state(unit)
    if st == "active":
        continue
    if seen.get(unit) == "reported":
        continue
    tail = ""
    if os.path.exists(log):
        try:
            tail = "\n".join(open(log, errors="replace").read().splitlines()[-6:])
        except Exception:
            pass
    if os.path.exists(result):
        head = "\n".join(open(result, errors="replace").read().splitlines()[:28])
        out.append(f"✅ {label} — unit {unit} finished (state: {st}).\nResult: {result}\n---\n{head}")
    else:
        out.append(f"⚠️ {label} — unit {unit} is NOT running (state: {st}) and wrote no result file "
                   f"({result}). Last log lines:\n{tail}")
    seen[unit] = "reported"

json.dump(seen, open(state_path, "w"), indent=1)
print("\n\n".join(out))
PY
