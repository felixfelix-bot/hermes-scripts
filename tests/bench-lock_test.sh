#!/usr/bin/env bash
# Tests for bench-lock.sh.
#
# The regression this pins: a lock holder that OUTLIVES the work it guards.
# On 2026-09-29 a lane held the bench lock with `while true; do sleep 30; done`,
# the lane finished, the holder did not, and the lock stayed held forever so the
# next lane could not use the hardware. These tests assert the lock is free
# after every way the holder can end - including being killed.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
LOCKER="$HERE/../bench-lock.sh"
TMP="$(mktemp -d)"
export BENCH_LOCK_FILE="$TMP/bench.lock"
pass=0; fail=0
ok()   { echo "ok   - $1"; pass=$((pass+1)); }
bad()  { echo "FAIL - $1"; fail=$((fail+1)); }

free() { timeout 3 flock -n "$BENCH_LOCK_FILE" -c 'exit 0' 2>/dev/null; }

# 1. starts free
free && ok "lock starts free" || bad "lock should start free"

# 2. run: holds while the command runs, releases afterwards
out=$("$LOCKER" run "t-run" -- sh -c 'echo ran')
[ "$out" = "ran" ] && ok "run executes the command" || bad "run did not execute the command: [$out]"
free && ok "run releases the lock afterwards" || bad "lock still held after run"

# 3. run: a failing command still releases the lock
"$LOCKER" run "t-fail" -- sh -c 'exit 7' >/dev/null 2>&1
rc=$?
[ "$rc" -eq 7 ] && ok "run propagates the command's exit code" || bad "run returned $rc, expected 7"
free && ok "run releases the lock after a failing command" || bad "lock still held after a failing command"

# 4. a held lock is visible as HELD, and run refuses rather than clobbering it.
#    (hold in a background subshell, bounded so the test cannot hang)
"$LOCKER" hold "t-held" --ttl 30 >/dev/null 2>&1 &
holder_pid=$!
sleep 1
"$LOCKER" status | grep -q HELD && ok "status reports HELD while a holder exists" || bad "status did not report HELD"
"$LOCKER" run "t-contend" -- sh -c 'exit 0' >/dev/null 2>&1
rc=$?
[ "$rc" -eq 1 ] && ok "run refuses (rc=1) when the lock is busy" || bad "run returned $rc on a busy lock, expected 1"

# 5. THE REGRESSION: killing the holder releases the lock.
kill -9 "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null
sleep 1
free && ok "killing the holder releases the lock" || bad "lock stayed held after the holder was killed"

# 6. bounded TTL: an oversized TTL is refused instead of creating a new leak.
"$LOCKER" hold "t-ttl" --ttl 999999 >/dev/null 2>&1
rc=$?
[ "$rc" -eq 2 ] && ok "oversized TTL is refused (rc=2)" || bad "oversized TTL returned $rc, expected 2"

# 7. usage error
"$LOCKER" bogus >/dev/null 2>&1
rc=$?
[ "$rc" -eq 2 ] && ok "unknown subcommand exits 2" || bad "unknown subcommand returned $rc, expected 2"

rm -rf "$TMP"
echo
echo "bench-lock_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
