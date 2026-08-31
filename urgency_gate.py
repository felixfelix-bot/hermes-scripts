#!/usr/bin/env python3
"""CG-11 urgency enforcement: create-gate, dispatch-gate, tick, tier, set.
Fail polarity: per-task fail-closed (unclassified never dispatches silently),
per-system fail-open (our bugs never stop dispatch). All mutations via hermes CLI."""
import json, os, re, sqlite3, subprocess, sys, time

HOME = os.path.expanduser("~")
BOARDS = f"{HOME}/.hermes/kanban/boards"
REAL = f"{HOME}/.hermes/hermes-agent/venv/bin/hermes"
LOG = f"{HOME}/.hermes/logs/urgency-gate.log"
TIER_CACHE = "/tmp/urgency_tier.json"
AUTODISPATCH = f"{HOME}/.hermes/config/urgency-autodispatch-boards.txt"
LEVELS = ("now", "soon", "defer", "batch")
DEFAULT_SOON_H = 6
OFFPEAK = set(range(22, 24)) | set(range(0, 6))  # UTC hours

def log(msg):
    line = f"[{time.strftime('%FT%TZ', time.gmtime())}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f: f.write(line + "\n")
    except Exception: pass
    print(msg)  # cron captures stdout

def alert(msg):
    subprocess.run(["logger", "-t", "urgency-gate", "--", f"ALERT {msg}"],
                   capture_output=True)
    log(f"ALERT {msg}")

def board_db(board):
    return f"{BOARDS}/{board}/kanban.db"

def resolve_board(args):
    if "--board" in args:
        return args[args.index("--board") + 1]
    return os.environ.get("HERMES_KANBAN_BOARD") or "default"

def sql(board, fn):
    """Run fn(conn) with busy_timeout; any error -> None (fail-open)."""
    try:
        conn = sqlite3.connect(board_db(board), timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        try: return fn(conn)
        finally: conn.close()
    except Exception as e:
        log(f"SQL fail-open ({board}): {e}")
        return None

def migrate(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "urgency" in have: return False
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency TEXT "
                 "CHECK (urgency IN ('now','soon','defer','batch'))")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_deadline INTEGER")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_set_at INTEGER")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_source TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(status, urgency)")
    conn.commit()
    return True

def kb(board, *args):
    env = dict(os.environ, HERMES_KANBAN_BOARD=board)
    return subprocess.run([REAL, "kanban"] + list(args), capture_output=True,
                          text=True, env=env, timeout=120)

# ---------- price tier (every input optional; missing -> skip) ----------
def _http_json(url, timeout=5):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None

def _env_has_key():
    try:
        # Build marker at runtime to avoid redactor corruption
        _m = "OPENROUTER" + "_OXALPHA" + "_" + "KEY="
        return _m in open(f"{HOME}/.hermes/.env").read()
    except Exception: return False

def price_tier():
    now = time.time()
    try:
        c = json.load(open(TIER_CACHE))
        if now - c.get("ts", 0) < 60: return c
    except Exception: pass
    ev, tier = [], "medium"  # unknown defaults to medium: soon passes, defer holds
    # free lane
    lane = False
    models = _http_json("http://localhost:9099/v1/models")
    if models and any("oxalpha" in str(m.get("id", "")).lower()
                      for m in models.get("data", [])):
        if os.path.exists("/tmp/ox_key_gate_ready") or _env_has_key():
            lane = True
    if lane:
        tier, ev = "free", ["oxalpha lane live"]
    else:
        q = _http_json("http://localhost:9099/quota") or {}
        w, h = _quota_pcts(q)
        if w is not None and h is not None:
            if w < 60 and h < 40:   tier, ev = "cheap",   [f"weekly={w}% 5h={h}%"]
            elif w < 85 and h < 75: tier, ev = "medium",  [f"weekly={w}% 5h={h}%"]
            else:                   tier, ev = "expensive", [f"weekly={w}% 5h={h}%"]
        else:
            ev.append("quota signal missing (medium default)")
    # paid-burn override
    spent = _paid_burn_1h()
    if spent is not None and spent >= 0.50 and tier != "free":
        tier, ev = "expensive", ev + [f"paid burn ${spent:.2f}/1h"]
    # hard gate (availability stays authoritative in staggered-dispatch)
    try:
        g = json.load(open(f"{HOME}/.hermes/state/rate_limit_gate.json"))
        if g.get("paused") and tier != "free":
            tier = "expensive"
            ev.append(f"hard gate paused: {g.get('reason', '?')[:60]}")
    except Exception: pass
    out = {"ts": now, "tier": tier, "evidence": ev}
    try: json.dump(out, open(TIER_CACHE, "w"))
    except Exception: pass
    return out

def _quota_pcts(q):
    w = h = None
    for k in ("friend", "ours"):
        for win in (q.get(k) or {}).get("windows", []):
            n = win.get("name", "").lower()
            if "week" in n and w is None: w = win.get("used_pct")
            if "5h" in n or "hour" in n and h is None: h = win.get("used_pct")
    if w is None or h is None:  # fallback: zai_state friend pct (both windows proxy)
        try:
            s = json.load(open(f"{HOME}/.hermes/bot/zai_state.json"))
            p = 100 - float(s.get("friend_token_pct", 100))
            w, h = (w if w is not None else p), (h if h is not None else p)
        except Exception: pass
    return w, h

def _paid_burn_1h():
    try:
        c = sqlite3.connect(f"file:{HOME}/.hermes/bot/api_burn.db?mode=ro", uri=True)
        row = c.execute("SELECT SUM(cost) FROM burn WHERE ts > ?",
                        (time.time() - 3600,)).fetchone()
        c.close()
        return float(row[0] or 0)
    except Exception:
        return None

RANK = {"free": 0, "cheap": 1, "medium": 2, "expensive": 3}

def eligible(urgency, tier, now=None):
    now = now or time.gmtime().tm_hour
    if urgency == "now": return True
    if urgency == "soon": return RANK[tier] <= RANK["medium"]
    if urgency == "defer": return RANK[tier] <= RANK["cheap"]
    if urgency == "batch":
        return tier == "free" or (tier == "cheap" and now in OFFPEAK)
    return False

# ---------- gates ----------
def park(board, tid, reason):
    r = kb(board, "schedule", tid, reason)
    ok = r.returncode == 0
    alert(("parked " if ok else "PARK FAILED ") + f"{tid} @ {board}: {reason}")
    return ok

def gate_create(argv):
    if "--urgency" in argv:
        lvl = argv[argv.index("--urgency") + 1].lower()
    elif sys.stdin.isatty():
        lvl = tty_ask(argv)
    else:
        sys.stderr.write(
            "URGENCY REQUIRED: pass --urgency now|soon|defer|batch "
            "(automation: set it explicitly or HERMES_URGENCY_EXEMPT=1, logged).\n")
        return 2
    if lvl not in LEVELS:
        sys.stderr.write(f"invalid --urgency '{lvl}' (use now|soon|defer|batch)\n"); return 2
    deadline = parse_deadline(argv) or (
        int(time.time()) + DEFAULT_SOON_H * 3600 if lvl == "soon" else None)
    args = strip_flags(argv, ("--urgency", "--urgency-deadline"))
    board = resolve_board(args)
    body_i = args.index("--body") + 1 if "--body" in args else None
    if body_i:
        args[body_i] = f"## Urgency: {lvl}\n" + args[body_i]
    if "--json" not in args: args.append("--json")
    r = run_real(["kanban"] + args)
    if r.returncode != 0:
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    stamp(board, r, lvl, deadline, source="operator" if sys.stdin.isatty() else "manager")
    sys.stdout.write(r.stdout)
    return 0

def tty_ask(argv):
    t = price_tier()
    print(f"Token price now: {t['tier'].upper()} ({'; '.join(t['evidence'])})")
    print("  now   — dispatch regardless of price (bleed/deadline)")
    print("  soon  — hours; waits for medium-or-cheaper, auto-escalates at deadline")
    print("  defer — days; waits for cheap only")
    print("  batch — cheapest window only (free lane / off-peak)")
    while True:
        a = input("urgency [now/soon/defer/batch]: ").strip().lower()
        if a in LEVELS: return a
        print("? use now|soon|defer|batch")

def stamp(board, r, lvl, deadline, source):
    """Simplified: find tasks created in the last 60s with NULL urgency on this board."""
    def w(conn):
        rows = conn.execute(
            "SELECT id FROM tasks WHERE urgency IS NULL AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 5",
            (int(time.time()) - 60,)).fetchall()
        ids = [row[0] for row in rows]
        for tid in ids:
            conn.execute("UPDATE tasks SET urgency=?, urgency_deadline=?, "
                         "urgency_set_at=?, urgency_source=? WHERE id=?",
                         (lvl, deadline, int(time.time()), source, tid))
        conn.commit()
        return ids
    ids = sql(board, w) or []
    log(f"classified {ids} @ {board} -> {lvl} (deadline={deadline}, {source})")

def gate_dispatch(argv):
    board = resolve_board(argv)
    try:
        t = price_tier()
        sql(board, migrate)
        ready = sql(board, lambda c: c.execute(
            "SELECT id, urgency FROM tasks WHERE status='ready'").fetchall()) or []
        for tid, urg in ready:
            if urg is None:
                park(board, tid, "urgency-unclassified: classify with "
                     f"hermes-urgency set {board} {tid} <now|soon|defer|batch>")
            elif not eligible(urg, t["tier"]):
                park(board, tid, f"price-hold: urgency={urg} not eligible at "
                     f"tier={t['tier']} ({'; '.join(t['evidence'])})")
    except Exception as e:
        log(f"dispatch-gate fail-open ({board}): {e}")  # never block dispatch on our bugs
    os.execv(REAL, [REAL] + argv)

def gate_promote(argv):
    board = resolve_board(argv)
    ids = [a for a in argv[3:] if a.startswith("t_")]
    ok = sql(board, lambda c: [i for (i,) in c.execute(
        "SELECT id FROM tasks WHERE id IN (%s) AND urgency IS NOT NULL" %
        ",".join("?" * len(ids)), ids)] or [])
    ok = [i for (i,) in (ok or [])]
    refuse = [i for i in ids if i not in ok]
    for tid in refuse:
        park(board, tid, "urgency-unclassified: promote refused until classified")
    if not ok:
        log(f"promote: nothing eligible ({ids} -> refused {refuse})"); return 0
    r = run_real(["kanban", "--board", board, "promote"] + ok)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    return r.returncode

# ---------- tick ----------
def tick():
    t = price_tier()
    log(f"tick tier={t['tier']} ({'; '.join(t['evidence'])})")
    enrolled = set()
    try: enrolled = {l.strip() for l in open(AUTODISPATCH) if l.strip()}
    except Exception: pass
    import glob as g
    for db in sorted(g.glob(f"{BOARDS}/*/kanban.db")):
        board = os.path.basename(os.path.dirname(db))
        try:
            sql(board, migrate)
            rows = sql(board, lambda c: c.execute(
                "SELECT id, status, urgency, urgency_deadline, created_at FROM tasks "
                "WHERE status IN ('ready','scheduled','todo')").fetchall()) or []
            for tid, st, urg, dl, cat in rows:
                if st == "ready" and urg is None:
                    park(board, tid, "urgency-unclassified (tick sweep)")
                elif st == "scheduled" and urg is not None and eligible(urg, t["tier"]):
                    kb(board, "unblock", tid, "--reason",
                       f"price window open: urgency={urg} tier={t['tier']}")
                    log(f"promoted {tid} @ {board} (tier={t['tier']})")
                elif (urg == "soon" and st != "running" and dl
                      and time.time() > dl):
                    def w(conn):
                        conn.execute("UPDATE tasks SET urgency='now', "
                                     "urgency_source='escalation', priority=MAX(priority,8) "
                                     "WHERE id=?", (tid,))
                        conn.commit()
                    sql(board, w)
                    kb(board, "comment", tid, "CG-11 escalation: SOON deadline passed "
                       "undelivered — promoted to NOW, dispatch next pass.")
                    alert(f"ESCALATED {tid} @ {board}: soon->now (deadline passed)")
            if board in enrolled:
                n = sql(board, lambda c: c.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone())
                if n and n[0][0]:
                    kb(board, "dispatch", "--max", "1")
        except Exception as e:
            log(f"tick fail-open ({board}): {e}")

# ---------- helpers ----------
def parse_deadline(argv):
    if "--urgency-deadline" not in argv: return None
    v = argv[argv.index("--urgency-deadline") + 1]
    m = re.match(r"\+(\d+(?:\.\d+)?)h$", v)
    if m: return int(time.time() + float(m.group(1)) * 3600)
    try:
        import calendar
        return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M"))
    except Exception: return None

def strip_flags(argv, flags):
    out, skip = [], False
    for a in argv:
        if skip: skip = False; continue
        if any(a == f or a.startswith(f + "=") for f in flags):
            skip = "=" not in a; continue
        out.append(a)
    return out

def run_real(args):
    return subprocess.run([REAL] + args, capture_output=True, text=True, timeout=300)

def cmd_set(argv):  # hermes-urgency set <board> <id> <lvl> [--deadline X] [--note ...]
    board, tid, lvl = argv[0], argv[1], argv[2].lower()
    if lvl not in LEVELS: sys.exit(f"bad level {lvl}")
    dl = parse_deadline(argv) or (
        int(time.time()) + DEFAULT_SOON_H * 3600 if lvl == "soon" else None)
    def w(conn):
        conn.execute("UPDATE tasks SET urgency=?, urgency_deadline=?, urgency_set_at=?, "
                     "urgency_source='operator' WHERE id=?", (lvl, dl, int(time.time()), tid))
        conn.commit()
    if sql(board, w) is None: sys.exit("db write failed")
    t = price_tier()
    r = sql(board, lambda c: c.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone())
    if r and r[0][0] == "scheduled" and eligible(lvl, t["tier"]):
        kb(board, "unblock", tid, "--reason", f"classified {lvl}; tier={t['tier']} OK")
    log(f"set {tid} @ {board} -> {lvl} (deadline={dl})")
    return 0

def main():
    a = sys.argv[1:]
    if not a: sys.exit("usage: urgency_gate.py gate|tick|tier|set|park-and-hold ...")
    if a[0] == "gate":
        sub = a[2] if len(a) > 2 else ""
        return {"create": gate_create, "dispatch": gate_dispatch,
                "promote": gate_promote}.get(sub, lambda av: (
                    os.execv(REAL, [REAL] + a)))(a[2:])
    if a[0] == "tick": tick(); return 0
    if a[0] == "tier":
        t = price_tier(); print(t["tier"], "-", "; ".join(t["evidence"])); return 0
    if a[0] == "set": return cmd_set(a[1:])
    if a[0] == "park-and-hold":  # staggered-dispatch pre-check (always exit 0)
        board = resolve_board(a)
        try:
            t = price_tier()
            for tid, urg in (sql(board, lambda c: c.execute(
                    "SELECT id, urgency FROM tasks WHERE status='ready'").fetchall()) or []):
                if urg is None:
                    park(board, tid, "urgency-unclassified (staggered pre-check)")
                elif not eligible(urg, t["tier"]):
                    park(board, tid, f"price-hold: urgency={urg} tier={t['tier']}")
        except Exception as e:
            log(f"park-and-hold fail-open ({board}): {e}")
        return 0
    sys.exit(f"unknown subcommand {a[0]}")

if __name__ == "__main__":
    sys.exit(main() or 0)
