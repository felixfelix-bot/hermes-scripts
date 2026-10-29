#!/usr/bin/env python3
"""M2a verification harness — real Nostr round trip against a sandbox board.

Proves, against the DEPLOYED script (imported as a module):
  1. to_epoch() converts the ISO-8601 TEXT timestamps that crashed outbound()
  2. outbound publish path works for a task with TEXT timestamps (was TypeError)
  3. event lands on a public relay (fetched back via nak req)
  4. inbound apply path writes the peer snapshot locally (LWW honoured)
  5. re-running inbound is a no-op (d-tag/ts dedupe)

Nothing outside the sandbox is touched: BOARDS_DIR/STATE_DIR are redirected and
hostname() is faked to simulate the peer machine.
"""
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import redirect_stdout

WS = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(WS, "sandbox")
BOARD = "synctest-t36ca6f0f"
# relay.damus.io, not nos.lol: from this host nos.lol resolves to an IPv6
# NAT64 address only and TCP/443 to it fails, so every relay-dependent check
# used to fail on a dead relay rather than on the code under test.
RELAY = os.environ.get("SYNC_TEST_RELAY", "wss://relay.damus.io")
DEPLOYED = os.environ.get(
    "SYNC_REPLICATE_PY",
    os.path.expanduser("~/.hermes/profiles/manager/scripts/kanban-nostr-replicate.py"))
LIVE_SCHEMA_FROM = os.path.expanduser("~/.hermes/kanban/boards/infrastructure/kanban.db")

FAILS = []
SKIPS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print(f"  [SKIP] {name} — {why}")
    SKIPS.append(name)


def relay_reachable(relay):
    """True when nak can open a REQ against the relay (connection only)."""
    try:
        r = subprocess.run(["nak", "req", "-k", "38010", "-l", "1", relay],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return "connecting to" in (r.stdout + r.stderr) and "ok." in (r.stdout + r.stderr)


def load_module():
    spec = importlib.util.spec_from_file_location("replicate", DEPLOYED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_sandbox():
    boards = os.path.join(SANDBOX, "boards", BOARD)
    state = os.path.join(SANDBOX, "state")
    os.makedirs(boards, exist_ok=True)
    os.makedirs(state, exist_ok=True)
    db = os.path.join(boards, "kanban.db")
    if os.path.exists(db):
        os.remove(db)
    src = sqlite3.connect(f"file:{LIVE_SCHEMA_FROM}?mode=ro", uri=True)
    ddl = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    src.close()
    con = sqlite3.connect(db)
    for stmt in ddl:
        try:
            con.execute(stmt)
        except sqlite3.Error:
            pass  # views/triggers referencing missing objects
    con.commit()
    return db, con


def main():
    mod = load_module()
    print("== 1. to_epoch() on the real-world values that crashed outbound()")
    cases = [
        ("2026-08-18T13:35:28Z", 1787060128),
        ("2026-08-18 19:42:40", None),
        ("2026-08-18T16:30Z", 1787070600),
        (1784203541, 1784203541),
        ("1784203541", 1784203541),
        (None, 0),
        ("garbage", 0),
    ]
    for raw, expect in cases:
        got = mod.to_epoch(raw)
        if expect is None:
            check(f"to_epoch({raw!r}) -> int", isinstance(got, int) and got > 0, f"got {got}")
        else:
            check(f"to_epoch({raw!r}) == {expect}", got == expect, f"got {got}")
    # the actual crash: max() over mixed str/int
    try:
        max(mod.to_epoch("2026-08-19 19:42:40"), 1784203541)
        check("max() over mixed text/int timestamps no longer raises", True)
    except TypeError as e:
        check("max() over mixed text/int timestamps no longer raises", False, str(e))

    db, con = build_sandbox()
    mod.BOARDS_DIR = os.path.join(SANDBOX, "boards")
    mod.STATE_DIR = os.path.join(SANDBOX, "state")
    mod.hostname = lambda: "CobradorWave"
    sk = mod.get_secret_key()
    check("nostr secret key loaded", bool(sk))
    if not sk:
        return finish()

    now = int(time.time())

    def insert_task(values):
        """Insert a task with only the columns this board's schema actually has."""
        cols = [r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()]
        use = {k: v for k, v in values.items() if k in cols}
        q = f"INSERT INTO tasks ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})"
        con.execute(q, list(use.values()))

    # T1: TEXT ISO timestamps (the crash reproducer)
    insert_task({
        "id": "t_synctest_iso", "title": "sandbox text-ts task", "body": "b",
        "assignee": "worker-admin", "status": "ready", "created_by": "harness",
        "created_at": "2026-08-18T13:35:28Z", "completed_at": "2026-08-19 19:42:40",
        "priority": 0,
    })
    # T2: integer timestamps (apply/LWW probe)
    insert_task({
        "id": "t_synctest_int", "title": "sandbox int-ts task", "body": "b",
        "assignee": "worker-admin", "status": "ready", "created_by": "harness",
        "created_at": now, "priority": 0,
    })
    con.commit()
    con.close()

    print("\n== 2. outbound --dry-run (previously: TypeError, run aborted)")
    r = subprocess.run(
        [sys.executable, DEPLOYED, "--outbound", "--dry-run"],
        capture_output=True, text=True, timeout=900,
        env={**os.environ})
    check("dry-run exits 0", r.returncode == 0, f"rc={r.returncode}")
    check("no traceback", "Traceback" not in r.stderr, r.stderr.strip()[-200:])

    reachable = relay_reachable(RELAY)
    RELAY_CHECKS = [
        "published t_synctest_iso", "published t_synctest_int",
        "relay returns sandbox events",
        "peer snapshot applied (title restored)",
        "peer snapshot applied (status restored)",
        "inbound reported an apply",
        "second inbound run is a no-op (dedupe)",
    ]
    if not reachable:
        print(f"\n== 3-5. relay-dependent checks SKIPPED — {RELAY} unreachable")
        for name in RELAY_CHECKS:
            skip(name, f"{RELAY} unreachable from this host")
    else:
        print("\n== 3. publish sandbox tasks to a real relay")
        for tid in ("t_synctest_iso", "t_synctest_int"):
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    mod.publish_single_task(sk, [RELAY], BOARD, tid)
                out = buf.getvalue().strip()
            except SystemExit:
                out = "SystemExit (publish failed)"
            check(f"published {tid}", "✅ Published" in out, out[-120:])

        print("\n== 4. event is retrievable from the relay")
        r = subprocess.run(["nak", "req", "-k", str(mod.KANBAN_KIND),
                            "-t", f"b={BOARD}", RELAY],
                           capture_output=True, text=True, timeout=60)
        ids = [json.loads(l).get("id") for l in r.stdout.strip().splitlines() if l.strip()]
        check("relay returns sandbox events", len(ids) >= 2, f"{len(ids)} event(s)")

        print("\n== 5. inbound apply as the PEER (hostname faked to DQ05)")
        # make the local copy stale so LWW must accept the peer snapshot
        con = sqlite3.connect(db)
        con.execute("UPDATE tasks SET title='LOCAL-STALE', status='blocked', created_at=1000000000,"
                    " started_at=NULL, completed_at=NULL WHERE id='t_synctest_int'")
        con.commit()
        con.close()

        mod.hostname = lambda: "c03rad0r-DQ05proplus"
        mod.BOARDS_DIR = os.path.join(SANDBOX, "boards")
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.inbound(sk, [RELAY])
        out1 = buf.getvalue()
        con = sqlite3.connect(db)
        row = con.execute("SELECT title,status,created_at FROM tasks WHERE id='t_synctest_int'").fetchone()
        con.close()
        check("peer snapshot applied (title restored)", row[0] == "sandbox int-ts task", str(row))
        check("peer snapshot applied (status restored)", row[1] == "ready", str(row))
        check("inbound reported an apply", "📥 Applied" in out1, out1.strip()[-120:])

        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.inbound(sk, [RELAY])
        out2 = buf.getvalue()
        check("second inbound run is a no-op (dedupe)", "📥 Applied" not in out2, out2.strip()[-120:])

    print("\n== 6. inbound dry-run against live state (no crash)")
    r = subprocess.run([sys.executable, DEPLOYED, "--inbound", "--dry-run"],
                       capture_output=True, text=True, timeout=300)
    check("live inbound dry-run exits 0", r.returncode == 0, f"rc={r.returncode}")
    check("live inbound dry-run no traceback", "Traceback" not in r.stderr,
          r.stderr.strip()[-200:])

    return finish()


def finish():
    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}"
              + (f" ({len(SKIPS)} skipped: {SKIPS})" if SKIPS else ""))
        return 1
    if SKIPS:
        print(f"RESULT: ALL EXECUTED CHECKS PASSED — {len(SKIPS)} SKIPPED: {SKIPS}")
        return 0
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
