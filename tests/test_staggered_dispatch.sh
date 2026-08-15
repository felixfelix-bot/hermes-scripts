#!/bin/bash
# test_staggered_dispatch.sh — T3.2 integration tests: board-pause markers,
# quota sweeper, 6h fail-safe canary, auto-resume.
#
# Legs use a stub hermes binary to assert ORCHESTRATION (which subcommands
# the dispatcher issues) and the real hermes CLI with a HERMES_KANBAN_DB pin
# for END-TO-END legs (sweep mode, pause full-run) — no leg can spawn a real
# worker.
#
# Run: bash tests/test_staggered_dispatch.sh   (from repo root or anywhere)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_UNDER_TEST="${SCRIPT_UNDER_TEST:-$HERE/../staggered-dispatch.sh}"
REAL_HERMES="${REAL_HERMES:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"

PASS=0
FAIL=0
SCRATCH_DIRS=()

# ---------- assert helpers ----------
ok()   { PASS=$((PASS + 1)); echo "  ok  - $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL- $1"; }
assert_eq() {  # actual expected label
    if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (actual=[$1] expected=[$2])"; fi
}
assert_contains() {  # haystack needle label
    case "$1" in *"$2"*) ok "$3" ;; *) bad "$3 (missing [$2] in [$(printf %.300s "$1")])" ;; esac
}
assert_not_contains() {
    case "$1" in *"$2"*) bad "$3 (unexpected [$2])" ;; *) ok "$3" ;; esac
}
assert_file_exists() {
    [ -f "$1" ] && ok "$2" || bad "$2 (missing $1)"
}
assert_file_absent() {
    [ ! -f "$1" ] && ok "$2" || bad "$2 (exists: $1)"
}

# ---------- fixture helpers ----------
new_env() {  # sets T (scratch root), STATE_DIR, BOARDS_ROOT, GATE_FILE, STUB, CALLS
    T="$(mktemp -d "$HERE/.sdscratch.XXXXXX")"
    SCRATCH_DIRS+=("$T")
    STATE_DIR="$T/state"
    BOARDS_ROOT="$T/boards"
    GATE_FILE="$STATE_DIR/rate_limit_gate.json"
    STUB="$T/bin/hermes-stub"
    CALLS="$T/calls.log"
    mkdir -p "$STATE_DIR" "$BOARDS_ROOT" "$T/bin"
    cat > "$STUB" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$CALLS"
exit 0
EOF
    chmod +x "$STUB"
}

write_gate() {  # paused resume_at reason
    printf '{"paused": %s, "resume_at": %s, "reason": "%s", "ts": "2026-08-15T10:00:00+00:00"}\n' \
        "$1" "${2:-null}" "$3" > "$GATE_FILE"
}

run_dispatch() {  # extra args; captures combined output in OUT, rc in RC
    # TEST_HERMES_BIN overrides the binary (stub by default). The dispatcher
    # that runs this test injects HERMES_BIN/HERMES_KANBAN_DB into worker
    # sessions — scrub both so legs are hermetic.
    OUT="$(env -u HERMES_BIN -u HERMES_KANBAN_DB \
        BOARDS="${BOARDS:-alpha beta}" \
        STATE_DIR="$STATE_DIR" GATE_FILE="$GATE_FILE" \
        KANBAN_BOARDS_ROOT="$BOARDS_ROOT" \
        HERMES_BIN="${TEST_HERMES_BIN:-$STUB}" \
        LOCK_FILE="$T/lock" SLEEP_BETWEEN=0 \
        LOAD_THRESHOLD=99 RAM_MIN_MB=1 \
        bash "$SCRIPT_UNDER_TEST" "$@" 2>&1)"
    RC=$?
}

stub_calls() { [ -f "$CALLS" ] && cat "$CALLS" || true; }
count_stub_calls() { stub_calls | grep -c -- "$1" || true; }

# real scratch board with a blocked quota-paused task + a control blocked task
# sets: BOARD_DB, QUOTA_TASK, CTRL_TASK, BOARD=alpha
make_scratch_board() {
    BOARD=alpha
    mkdir -p "$BOARDS_ROOT/$BOARD"
    BOARD_DB="$BOARDS_ROOT/$BOARD/kanban.db"
    local envpin=(env HERMES_KANBAN_DB="$BOARD_DB")
    "${envpin[@]}" "$REAL_HERMES" kanban init >/dev/null 2>&1
    QUOTA_TASK="$("${envpin[@]}" "$REAL_HERMES" kanban create "quota blocked task" --assignee test-worker 2>/dev/null | grep -oE 't_[0-9a-f]{8}' | head -1)"
    CTRL_TASK="$("${envpin[@]}" "$REAL_HERMES" kanban create "review blocked task" --assignee test-worker 2>/dev/null | grep -oE 't_[0-9a-f]{8}' | head -1)"
    "${envpin[@]}" "$REAL_HERMES" kanban block "$QUOTA_TASK" "quota-paused: gate says zai-503-outage, resume_at 2026-08-15T12:00:00+00:00 — no work lost, not a task defect" >/dev/null 2>&1
    "${envpin[@]}" "$REAL_HERMES" kanban block "$CTRL_TASK" "review-required: needs human eyes" >/dev/null 2>&1
}

task_status() {  # db task_id -> prints status
    python3 - "$1" "$2" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT status FROM tasks WHERE id=?", (sys.argv[2],)).fetchone()
print(row[0] if row else "MISSING")
PY
}
task_failures() {
    python3 - "$1" "$2" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (sys.argv[2],)).fetchone()
print(row[0] if row else "MISSING")
PY
}

marker_for() { echo "$STATE_DIR/board_pause_$1"; }

fabricate_marker() {  # board age_seconds  -> marker with given age
    python3 - "$STATE_DIR" "$1" "$2" <<'PY'
import json, os, sys, time
state_dir, board, age = sys.argv[1], sys.argv[2], int(sys.argv[3])
now = time.time()
marker = {
    "board": board,
    "paused_at_epoch": now - age,
    "paused_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age)),
    "updated_at_epoch": now,
    "reason": "zai-503-outage: fabricated",
    "resume_at": None,
    "alerted_2h": False,
    "canary_at_epoch": None,
    "canary_count": 0,
}
with open(os.path.join(state_dir, f"board_pause_{board}"), "w") as f:
    json.dump(marker, f)
PY
}

marker_field() {  # board key -> prints value
    python3 - "$STATE_DIR" "$1" "$2" <<'PY'
import json, sys
m = json.load(open(f"{sys.argv[1]}/board_pause_{sys.argv[2]}"))
print(m.get(sys.argv[3]))
PY
}

# ================= TESTS =================

t1_pause_zai_writes_markers_skips_dispatch() {
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: 4 server errors in 600s"
    run_dispatch
    assert_eq "$RC" "0" "t1: exit 0 under zai pause"
    assert_file_exists "$(marker_for alpha)" "t1: marker for board alpha"
    assert_file_exists "$(marker_for beta)" "t1: marker for board beta"
    assert_eq "$(count_stub_calls 'dispatch')" "0" "t1: NO dispatch calls under pause"
    assert_contains "$OUT" "board-pause" "t1: log mentions board-pause"
    assert_eq "$(marker_field alpha reason)" "zai-503-outage: 4 server errors in 600s" "t1: marker carries gate reason"
    assert_eq "$(marker_field alpha resume_at)" "2026-08-15T12:00:00+00:00" "t1: marker carries resume_at"
}

t2_pause_429_also_quota_class() {
    new_env
    write_gate true '"2026-08-15T11:31:02+00:00"' "ACTIVE 429: 3 429(s) in last 300s (rls=3, api=0)"
    run_dispatch
    assert_file_exists "$(marker_for alpha)" "t2: ACTIVE 429 pause writes marker"
    assert_eq "$(count_stub_calls 'dispatch')" "0" "t2: no dispatch under 429 pause"
}

t3_pause_unknown_reason_no_markers_still_skips() {
    new_env
    write_gate true null "mystery outage"
    run_dispatch
    assert_eq "$RC" "0" "t3: exit 0 under unknown pause"
    assert_file_absent "$(marker_for alpha)" "t3: no marker for unknown reason"
    assert_eq "$(count_stub_calls 'dispatch')" "0" "t3: still no dispatch (fail-closed on any pause)"
    assert_contains "$OUT" "unknown pause reason" "t3: warns about unknown reason"
}

t4_clear_removes_markers_sweeps_dispatches() {
    new_env
    make_scratch_board
    fabricate_marker alpha 300
    fabricate_marker beta 300
    write_gate false null "clear"
    run_dispatch
    assert_file_absent "$(marker_for alpha)" "t4: marker alpha removed on clear"
    assert_file_absent "$(marker_for beta)" "t4: marker beta removed on clear"
    assert_contains "$OUT" "marker removed" "t4: logs marker removal (auto-resume)"
    assert_eq "$(count_stub_calls 'dispatch')" "2" "t4: dispatch called for both boards"
    assert_contains "$(stub_calls)" "--board alpha dispatch --max 1" "t4: alpha dispatch args preserved"
    assert_contains "$(stub_calls)" "unblock" "t4: sweeper invoked (stub records unblock attempt)"
}

t5_advisory_clear_dispatches() {
    new_env
    write_gate false null "ADVISORY: hour 11Z is a known rate-limit peak hour"
    run_dispatch
    assert_eq "$(count_stub_calls 'dispatch')" "2" "t5: advisory (paused=false) dispatches"
    assert_file_absent "$(marker_for alpha)" "t5: no marker when not paused"
}

t6_missing_gate_fail_open() {
    new_env
    rm -f "$GATE_FILE"
    run_dispatch
    assert_eq "$(count_stub_calls 'dispatch')" "2" "t6: missing gate file -> fail-open dispatch"
    assert_file_absent "$(marker_for alpha)" "t6: no markers on fail-open"
    assert_eq "$(count_stub_calls 'unblock')" "0" "t6: no sweep on fail-open (gate not confirmed clear)"
}

t7_sweep_mode_real_cli_requeues_quota_task() {
    new_env
    make_scratch_board
    local failures_before
    failures_before="$(task_failures "$BOARD_DB" "$QUOTA_TASK")"
    write_gate false null "clear"
    BOARDS="alpha" TEST_HERMES_BIN="$REAL_HERMES" run_dispatch --sweep
    assert_eq "$RC" "0" "t7: sweep mode exit 0"
    assert_eq "$(task_status "$BOARD_DB" "$QUOTA_TASK")" "ready" "t7: quota-paused task re-queued to ready"
    assert_eq "$(task_failures "$BOARD_DB" "$QUOTA_TASK")" "$failures_before" "t7: failures counter unchanged"
    assert_eq "$(task_status "$BOARD_DB" "$CTRL_TASK")" "blocked" "t7: review-required task STAYS blocked"
    assert_contains "$OUT" "sweeper" "t7: sweeper log line present"
}

t8_sweep_mode_skips_when_gate_paused() {
    new_env
    make_scratch_board
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: 4 server errors in 600s"
    BOARDS="alpha" TEST_HERMES_BIN="$REAL_HERMES" run_dispatch --sweep
    assert_eq "$(task_status "$BOARD_DB" "$QUOTA_TASK")" "blocked" "t8: sweep does nothing while gate paused"
    assert_not_contains "$OUT" "unblocked" "t8: no unblock action logged"
}

t9_full_run_real_cli_pause_no_spawn() {
    # real hermes binary, paused gate: nothing must be invoked at all.
    new_env
    make_scratch_board
    local runs_before
    runs_before="$(python3 - "$BOARD_DB" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM task_runs").fetchone()[0])
PY
)"
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: live pause"
    BOARDS="alpha" TEST_HERMES_BIN="$REAL_HERMES" run_dispatch
    assert_eq "$RC" "0" "t9: real-CLI pause run exits 0"
    assert_file_exists "$(marker_for alpha)" "t9: marker written via real run"
    assert_eq "$(task_status "$BOARD_DB" "$QUOTA_TASK")" "blocked" "t9: blocked task untouched"
    local runs
    runs="$(python3 - "$BOARD_DB" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM task_runs").fetchone()[0])
PY
)"
    assert_eq "$runs" "$runs_before" "t9: task_runs unchanged (no claims, no failure accounting)"
}

t10_failsafe_canary_after_6h() {
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: stuck"
    fabricate_marker alpha 25200   # 7h
    fabricate_marker beta 25200
    run_dispatch
    assert_eq "$(count_stub_calls 'dispatch --max 1')" "1" "t10: exactly ONE canary claim forced"
    assert_contains "$(stub_calls)" "--board alpha dispatch --max 1" "t10: canary targets first paused board"
    assert_contains "$OUT" "FAILSAFE" "t10: fail-safe alert raised"
    assert_contains "$OUT" "canary" "t10: canary logged"
    assert_eq "$(marker_field alpha canary_count)" "1" "t10: canary_count committed"
    # second pass within 6h: no new canary
    run_dispatch
    assert_eq "$(count_stub_calls 'dispatch --max 1')" "1" "t10: no second canary within interval"
}

t11_no_canary_before_6h() {
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: fresh"
    fabricate_marker alpha 10800   # 3h
    run_dispatch
    assert_eq "$(count_stub_calls 'dispatch')" "0" "t11: no dispatch/canary before 6h"
    assert_not_contains "$OUT" "FAILSAFE" "t11: no fail-safe alert before 6h"
}

t12_alert_2h_fires_once() {
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: long"
    fabricate_marker alpha 9000    # 2.5h
    run_dispatch
    assert_contains "$OUT" "ALERT" "t12: 2h manager alert raised"
    assert_contains "$OUT" "board-paused >2h" "t12: alert names the >2h condition"
    assert_eq "$(marker_field alpha alerted_2h)" "True" "t12: alerted_2h committed"
    local first_count
    first_count="$(printf '%s\n' "$OUT" | grep -c '^ALERT ' || true)"
    run_dispatch
    assert_eq "$(printf '%s\n' "$OUT" | grep -c '^ALERT ' || true)" "0" "t12: alert NOT repeated on second pass"
    assert_eq "$first_count" "1" "t12: exactly one alert line first time"
}

t13_marker_preserves_paused_at_across_passes() {
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' "zai-503-outage: episode"
    run_dispatch
    local first
    first="$(marker_field alpha paused_at_epoch)"
    sleep 1
    run_dispatch
    assert_eq "$(marker_field alpha paused_at_epoch)" "$first" "t13: paused_at preserved across passes"
}

t14_quota_window_and_kalman_reasons_are_quota_class() {
    new_env
    write_gate true '"2026-08-16T05:00:00+00:00"' "QUOTA-WINDOW: 5h window at 91% used"
    run_dispatch
    assert_file_exists "$(marker_for alpha)" "t14: QUOTA-WINDOW writes marker"
    new_env
    write_gate true '"2026-08-15T11:00:00+00:00"' "KALMAN: 5h window predicts exhaustion in 42 min"
    run_dispatch
    assert_file_exists "$(marker_for alpha)" "t14: KALMAN writes marker"
}

t15_tab_in_reason_does_not_shift_fields() {
    # regression: gate reason containing a tab must not corrupt
    # GATE_REASON/GATE_RESUME_AT field parsing (reason is the LAST field).
    # JSON source uses the escaped form 	 (raw control chars are invalid
    # JSON); json.load decodes it to a real tab before the dispatcher sees it.
    new_env
    write_gate true '"2026-08-15T12:00:00+00:00"' 'zai-503-outage: a	b'
    local expected='zai-503-outage: a'$'	''b'
    run_dispatch
    assert_file_exists "$(marker_for alpha)" "t15: marker written despite tab in reason"
    assert_eq "$(marker_field alpha reason)" "$expected" "t15: full tabbed reason preserved"
    assert_eq "$(marker_field alpha resume_at)" "2026-08-15T12:00:00+00:00" "t15: resume_at not shifted by tab"
}

# ---------- run ----------
echo "== T3.2 staggered-dispatch integration tests =="
TEST_FILTER="${TESTS:-}"
for t in $(declare -F | awk '{print $3}' | grep '^t[0-9]*_'); do
    [ -n "$TEST_FILTER" ] && case " $TEST_FILTER " in *" $t "*) ;; *) continue ;; esac
    echo "-- $t"
    "$t"
done
echo
echo "PASS=$PASS FAIL=$FAIL"
# cleanup scratch
for d in "${SCRATCH_DIRS[@]:-}"; do
    [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d"
done
[ "$FAIL" -eq 0 ] || exit 1
