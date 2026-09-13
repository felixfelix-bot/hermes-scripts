"""Ad-hoc verification for ctx_hygiene_check.py / ctx_hygiene_summary.py.

Builds a synthetic kanban fixture in a throwaway HOME and asserts both scripts
report the exact expected stale counts / buckets, plus a live cross-check
against the real boards (script output vs independent sqlite3 CLI oracle).
"""
import os, re, sqlite3, subprocess, sys, tempfile, time

SCRIPTS = os.path.expanduser("~/.hermes/profiles/manager/scripts")
CHECK = os.path.join(SCRIPTS, "ctx_hygiene_check.py")
SUMM = os.path.join(SCRIPTS, "ctx_hygiene_summary.py")
now = int(time.time())
fail = []


def d(days):
    return now - int(days * 86400)


SCHEMA = """CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT,
 assignee TEXT, status TEXT NOT NULL, priority INTEGER, created_by TEXT,
 created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
 block_kind TEXT, last_failure_error TEXT);"""

# (id, status, age_days, assignee)  -> stale = age>=7 and status open
FIXTURE = [
    ("f_fresh", "todo", 1, "worker-x"),        # not stale
    ("f_8d", "blocked", 8, "worker-x"),        # stale
    ("f_31d", "todo", 31, "worker-y"),         # stale, >30
    ("f_65d", "blocked", 65, "worker-z"),      # stale, >60
    ("f_done", "done", 40, "worker-x"),        # terminal -> excluded
    ("f_arch", "archived", 90, "worker-x"),    # terminal -> excluded
]
EXPECT_STALE, EXPECT_30, EXPECT_60 = 3, 2, 1


def build_fixture(home):
    bdir = os.path.join(home, ".hermes", "kanban", "boards")
    for b in ("admin", "market", "tollgate"):
        p = os.path.join(bdir, b)
        os.makedirs(p, exist_ok=True)
        con = sqlite3.connect(os.path.join(p, "kanban.db"))
        con.execute(SCHEMA)
        if b == "admin":  # only admin gets the known fixture cards
            con.executemany(
                "INSERT INTO tasks(id,title,status,created_at,assignee,block_kind)"
                " VALUES(?,?,?,?,?,'')",
                [(i, "title " + i, s, d(a), asg) for i, s, a, asg in FIXTURE])
        con.commit(); con.close()
    return bdir


def run(script, home):
    env = dict(os.environ, HOME=home)
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    r.check_returncode()
    return r.stdout


def band(out, board):
    m = re.search(r"^### %s: total=\d+ open=(\d+) stale7=(\d+) \(>30d=(\d+), >60d=(\d+)\)" % board,
                  out, re.M)
    return tuple(int(x) for x in m.groups()) if m else None


def live_oracle():
    """Independent sqlite3 CLI count for each real board."""
    out = {}
    for b in ("admin", "market", "tollgate"):
        db = os.path.expanduser("~/.hermes/kanban/boards/%s/kanban.db" % b)
        q = ("select count(*) from tasks where created_at < strftime('%s','now')-7*86400 "
             "and status not in ('done','completed','archived','cancelled');")
        out[b] = int(subprocess.run(["sqlite3", db, q], capture_output=True, text=True).stdout.strip())
    return out


tmp_root = os.path.expanduser("~/.tmp")
os.makedirs(tmp_root, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="hermes-verify-ctx-", dir=tmp_root) as tmp:
    build_fixture(tmp)
    chk = run(CHECK, tmp)
    sm = run(SUMM, tmp)

    # --- assertions on fixture (synthetic, known-truth) ---
    m = re.search(r"=== admin === total=(\d+).*?stale>7d=(\d+)", chk, re.S)
    got_total, got_stale = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    if got_total != len(FIXTURE):
        fail.append("check total: got %r want %d" % (got_total, len(FIXTURE)))
    if got_stale != EXPECT_STALE:
        fail.append("check stale>7d: got %r want %d" % (got_stale, EXPECT_STALE))
    for tid in ("f_8d", "f_31d", "f_65d"):
        if tid not in chk:
            fail.append("check missing stale card %s" % tid)
    for tid in ("f_fresh", "f_done", "f_arch"):
        if tid in chk:
            fail.append("check wrongly listed %s" % tid)

    b = band(sm, "admin")
    expect_open = sum(1 for _, s, _, _ in FIXTURE if s not in ("done", "completed", "archived", "cancelled"))
    if b != (expect_open, EXPECT_STALE, EXPECT_30, EXPECT_60):
        fail.append("summary admin band: got %r want (%d,%d,%d,%d)"
                    % (b, expect_open, EXPECT_STALE, EXPECT_30, EXPECT_60))

    # --- live cross-check against real boards ---
    real = run(SUMM, os.path.expanduser("~"))
    oracle = live_oracle()
    for board, want in oracle.items():
        got = band(real, board)
        if got is None or got[1] != want:
            fail.append("live %s: summary stale=%r oracle=%d" % (board, got and got[1], want))

print("oracle(live sqlite3) =", oracle)
print("summary band        =", {k: band(real, k) for k in oracle})
if fail:
    print("FAIL"); [print("  -", f) for f in fail]; sys.exit(1)
print("PASS: fixture truth + live oracle agree")
