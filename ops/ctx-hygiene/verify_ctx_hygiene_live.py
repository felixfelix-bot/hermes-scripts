"""Ad-hoc verification harness for the ctx-hygiene scripts.

Runs the committed verifier (repo copy) plus an independent oracle check of the
LIVE scripts in ~/.hermes/profiles/manager/scripts, and asserts the two copies
are byte-identical so the published artifact matches what the cron runs.
"""
import hashlib, os, re, subprocess, sys, tempfile

LIVE = os.path.expanduser("~/.hermes/profiles/manager/scripts")
REPO = os.path.expanduser("~/repos/hermes-scripts/ops/ctx-hygiene")
fail = []


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# 1. published artifact == live script (no drift between repo and cron)
for name in ("ctx_hygiene_check.py", "ctx_hygiene_summary.py"):
    if sha(os.path.join(LIVE, name)) != sha(os.path.join(REPO, name)):
        fail.append("drift: %s differs between live and repo copy" % name)

# 2. run the committed verifier (fixture truth + live oracle)
r = subprocess.run([sys.executable, os.path.join(REPO, "verify_ctx_hygiene.py")],
                   capture_output=True, text=True)
print(r.stdout.strip())
if r.returncode != 0:
    fail.append("committed verifier returned %d" % r.returncode)

# 3. independent oracle straight off the LIVE scripts
env = dict(os.environ)
for board in ("admin", "market", "tollgate"):
    db = os.path.expanduser("~/.hermes/kanban/boards/%s/kanban.db" % board)
    q = ("select count(*) from tasks where created_at < strftime('%s','now')-7*86400 "
         "and status not in ('done','completed','archived','cancelled');")
    want = int(subprocess.run(["sqlite3", db, q], capture_output=True, text=True).stdout.strip())
    live = subprocess.run([sys.executable, os.path.join(LIVE, "ctx_hygiene_summary.py")],
                          capture_output=True, text=True, env=env).stdout
    m = re.search(r"^### %s: total=\d+ open=\d+ stale7=(\d+)" % board, live, re.M)
    got = int(m.group(1)) if m else None
    if got != want:
        fail.append("live %s stale7=%r oracle=%d" % (board, got, want))
    else:
        print("live %-8s stale7=%d == oracle" % (board, got))

print("FAIL: " + "; ".join(fail) if fail else "PASS: artifact matched, fixture truth + live oracle agree")
sys.exit(1 if fail else 0)
