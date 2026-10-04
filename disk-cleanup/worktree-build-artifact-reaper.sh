#!/bin/bash
# worktree-build-artifact-reaper.sh — night-guard reaper for per-task build trees
# ---------------------------------------------------------------------------
# Reclaims the regenerable build trees that per-task kanban workspaces leave
# behind:  <workspace>/{target,node_modules,.pio,build}
#
# Origin: kanban t_f841919b (2026-09-13).  Root cause it addresses: every kanban
# task gets a fresh worktree, installs ~0.1-1G node_modules and/or builds a
# multi-GB cargo/PlatformIO tree, and nothing prunes it when the task ends —
# disk hits CRITICAL every 3-4 days (t_2c530663, t_dcee015c, t_f978b464).
#
# SAFETY CONTRACT (enforced structurally, not by convention)
#   * the ONLY paths this script can ever delete are exactly
#     <workspace>/{target,node_modules,.pio,build}; it never deletes a
#     workspace, repo, worktree, DB, named docker volume, or user file;
#   * policy engine: reaper_plan.py (same directory).  It protects any path
#     under an active card workspace, requires a terminal card status (or an
#     orphan workspace), a stale age (>= --stale-days, hard floor 48h),
#     no open fds, no process cwd/cmdline inside the tree, no git-tracked
#     content inside the tree, no live compiler/build process working in the
#     unit, and - the HARD GATE from Felix 2026-09-13 - proof that the unit's
#     work is consolidated upstream (clean `git status`, HEAD contained by a
#     remote branch).  Units that cannot prove that are logged `needs-decision`
#     and left alone;
#   * dry-run is the DEFAULT.  Deletion requires an explicit --execute.
#   * every deletion is re-verified immediately before `rm -rf` and logged.
#
# USAGE
#   bash worktree-build-artifact-reaper.sh [--execute] [--verbose] [...]
#     --execute                 actually delete (default: plan only)
#     --dry-run                 alias for plan-only (never deletes)
#     --stale-days N            build tree must be untouched N days (default 7, floor 2)
#     --include-blocked         ALSO reap build trees in `blocked` workspaces
#                               (policy: APPROVED by Felix 2026-09-13,
#                               conservative - still OFF by default, enable on
#                               the crontab line)
#     --blocked-stale-days N    age gate for the above (default 30)
#     --no-projects             do not scan long-lived checkouts (~/repos,
#                               home-root project dirs); they are scanned by
#                               default with the same guards
#     --allow-non-git           permit units that are not git repos (default:
#                               needs-decision, because consolidation cannot be
#                               proven for them)
#     --min-size-mb N           ignore trees smaller than N MB (default 50)
#     --budget-mb N             max MB deleted per run (default 8000)
#     --only-if-free-gb N       do nothing when free space >= N GB (default 45, 0=always)
#     --log FILE                log file (default ~/.hermes/logs/worktree-build-reaper.log)
#     --plan FILE               plan TSV output (default alongside the log)
#     --protect-file FILE       newline-separated extra protected paths
#     --summary FILE            JSON summary (default alongside the log)
#     --verbose                 log the plan too, and echo a summary line on stdout
#
# OUTPUT CONTRACT (watchdog pattern, matches unified-system-alert.sh):
#   stdout is EMPTY on success, and carries a short ALERT block on failure.
#   Schedule as:  hermes cron create --no-agent --deliver <chat> \
#                      --script worktree-build-reaper.sh '20 4 * * *'
#   or a user crontab entry (see --log).
#
# Exit: 0 ok, 1 failure (bad deletion / internal error), 2 usage error.
# ---------------------------------------------------------------------------
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER="$SELF_DIR/reaper_plan.py"

EXECUTE=0
VERBOSE=0
STALE_DAYS=7
BLOCKED_STALE_DAYS=30
INCLUDE_BLOCKED=0
PROJECTS=1
ALLOW_NON_GIT=0
MIN_SIZE_MB=50
BUDGET_MB=8000
ONLY_IF_FREE_GB=45
LOGDIR="${HOME}/.hermes/logs"
LOG="${LOGDIR}/worktree-build-reaper.log"
PLAN=""
SUMMARY=""
PROTECT_FILE="${HOME}/.hermes/kanban/reaper-protect.txt"

# Count holder lines in raw `lsof +D` output (stdin -> stdout).
# lsof always prints a "COMMAND PID ..." header and prints *nothing at all*
# when there are no holders (rc=1).  Counting must therefore strip the header
# and count only non-empty lines: `grep -vc COMMAND` counts the single empty
# line of empty output as one holder and vetoes EVERY deletion.
count_holders() { grep -v '^COMMAND' | grep -c . ; }

if [ "${1:-}" = "--self-test" ]; then
  st_ok=0; st_bad=0
  chk() {
    got=$(printf '%s\n' "$2" | count_holders)
    if [ "$got" = "$3" ]; then st_ok=$((st_ok+1))
    else st_bad=$((st_bad+1)); printf 'FAIL self-test %s: got %s want %s\n' "$1" "$got" "$3"; fi
  }
  chk empty-output "" 0
  chk header-only "COMMAND     PID     USER   FD   TYPE DEVICE SIZE/OFF    NODE NAME" 0
  chk one-holder "COMMAND     PID     USER   FD   TYPE DEVICE SIZE/OFF    NODE NAME
python3.1 2720453 c03rad0r 3w   REG  259,2 51332456 4981321 /p/node_modules/held.txt" 1
  printf 'self-test: %d ok, %d failed\n' "$st_ok" "$st_bad"
  [ "$st_bad" = 0 ] || exit 1
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --dry-run|--plan-only) EXECUTE=0 ;;
    --verbose|-v) VERBOSE=1 ;;
    --include-blocked) INCLUDE_BLOCKED=1 ;;
    --no-projects) PROJECTS=0 ;;
    --projects) PROJECTS=1 ;;
    --allow-non-git) ALLOW_NON_GIT=1 ;;
    --stale-days) STALE_DAYS="${2:-}"; shift ;;
    --blocked-stale-days) BLOCKED_STALE_DAYS="${2:-}"; shift ;;
    --min-size-mb) MIN_SIZE_MB="${2:-}"; shift ;;
    --budget-mb) BUDGET_MB="${2:-}"; shift ;;
    --only-if-free-gb) ONLY_IF_FREE_GB="${2:-}"; shift ;;
    --log) LOG="${2:-}"; shift ;;
    --plan) PLAN="${2:-}"; shift ;;
    --summary) SUMMARY="${2:-}"; shift ;;
    --protect-file) PROTECT_FILE="${2:-}"; shift ;;
    -h|--help) sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "usage: $(basename "$0") [--execute] [--verbose] [--stale-days N] [--include-blocked]" >&2; exit 2 ;;
  esac
  shift
done

for v in "$STALE_DAYS" "$BLOCKED_STALE_DAYS" "$MIN_SIZE_MB" "$BUDGET_MB" "$ONLY_IF_FREE_GB"; do
  case "$v" in ''|*[!0-9]*) echo "usage: numeric arg expected, got '$v'" >&2; exit 2 ;; esac
done

mkdir -p "$LOGDIR"
[ -n "$PLAN" ] || PLAN="${LOG%.log}.plan.tsv"
[ -n "$SUMMARY" ] || SUMMARY="${LOG%.log}.summary.json"

# log rotation (keep one previous copy)
if [ -f "$LOG" ]; then
  sz=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  [ "$sz" -gt 5242880 ] && mv -f "$LOG" "$LOG.1"
fi

exec 9>"${LOG%.log}.lock"
if ! flock -n 9; then
  printf '%s another reaper run holds the lock; exiting\n' "$(date '+%F %T')" >>"$LOG"
  exit 0
fi

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
say() { [ "$VERBOSE" = 1 ] && printf '%s\n' "$*"; :; }
df1() { df -h / | tail -1; }
avail_gb() { df -BG / | tail -1 | awk '{print $4}' | tr -d 'G'; }
FAILED=0

log "=== worktree build-artifact reaper: execute=$EXECUTE verbose=$VERBOSE stale=${STALE_DAYS}d blocked=${INCLUDE_BLOCKED}/${BLOCKED_STALE_DAYS}d projects=$PROJECTS allow-non-git=$ALLOW_NON_GIT min=${MIN_SIZE_MB}M budget=${BUDGET_MB}M ==="
log "BEFORE df -h: $(df1)"

if [ "$ONLY_IF_FREE_GB" -gt 0 ]; then
  free_gb=$(avail_gb)
  if [ "${free_gb:-999}" -ge "$ONLY_IF_FREE_GB" ]; then
    log "headroom sufficient: ${free_gb}G free >= ${ONLY_IF_FREE_GB}G -> no action"
    log "=== done (no action) ==="
    exit 0
  fi
  log "free ${free_gb}G < ${ONLY_IF_FREE_GB}G gate -> reaping"
fi

if [ ! -f "$PLANNER" ]; then
  log "FATAL: policy engine missing: $PLANNER"
  printf 'WORKTREE-REAPER FAILURE: policy engine missing: %s\n' "$PLANNER"
  exit 1
fi

plan_args=(--stale-days "$STALE_DAYS" --blocked-stale-days "$BLOCKED_STALE_DAYS"
           --min-size-mb "$MIN_SIZE_MB" --budget-mb "$BUDGET_MB"
           --protect-file "$PROTECT_FILE" --summary "$SUMMARY")
[ "$INCLUDE_BLOCKED" = 1 ] && plan_args+=(--include-blocked)
[ "$PROJECTS" = 1 ] || plan_args+=(--no-projects)
[ "$ALLOW_NON_GIT" = 1 ] && plan_args+=(--allow-non-git)

live_builds=$(pgrep -c -f 'cargo|rustc|pio run|platformio|go build|next dev|vite|webpack|esbuild|tsc ' 2>/dev/null || echo 0)
log "live build-ish processes (global, informational): ${live_builds}"

if ! python3 "$PLANNER" "${plan_args[@]}" >"$PLAN" 2>"${PLAN}.err"; then
  log "FATAL: planner failed: $(tail -3 "${PLAN}.err")"
  printf 'WORKTREE-REAPER FAILURE: planner failed (%s)\n' "$(tail -1 "${PLAN}.err")"
  exit 1
fi
[ -s "${PLAN}.err" ] && log "planner stderr: $(tail -3 "${PLAN}.err")"

total_del=$(grep -c $'^DELETE\t' "$PLAN" 2>/dev/null || true); total_del=${total_del:-0}
total_skip=$(grep -c $'^SKIP\t' "$PLAN" 2>/dev/null || true); total_skip=${total_skip:-0}
del_mb=$(awk -F'\t' '$1=="DELETE"{s+=$2} END{print s+0}' "$PLAN")
log "plan: $total_del candidate trees (${del_mb}M) / $total_skip skipped"
# Guards that refused work: the HARD consolidation gate (Felix 2026-09-13) and
# the live-compiler guard.  Surfaced line-by-line so a human can review them.
nd_count=$(awk -F'\t' '$1=="SKIP" && $5 ~ /^needs-decision/ {n++} END{print n+0}' "$PLAN")
lb_count=$(awk -F'\t' '$1=="SKIP" && $5 ~ /^live-build-proc/ {n++} END{print n+0}' "$PLAN")
log "guards: ${nd_count} needs-decision (consolidation/board-text), ${lb_count} live-build-proc"
if [ "$nd_count" -gt 0 ]; then
  awk -F'\t' '$1=="SKIP" && $5 ~ /^needs-decision/ {printf "NEEDS-DECISION  %s  ...%s\n",$5,substr($4,length($4)-55)}' "$PLAN" >>"$LOG"
fi
[ "$VERBOSE" = 1 ] && awk -F'\t' '{printf "%s\t%6sM\t%s\t%s\n",$1,$2,substr($4,length($4)-70),$5}' "$PLAN" >>"$LOG"
[ "$VERBOSE" = 0 ] && awk -F'\t' '$1=="SKIP" && $2>=200 {printf "%s\t%6sM\t%s\n",$1,$2,substr($4,length($4)-70)}' "$PLAN" >>"$LOG"

if [ "$EXECUTE" != 1 ]; then
  log "DRY RUN — nothing deleted."
  log "AFTER df -h: $(df1)"
  log "=== done (dry-run) ==="
  say "dry-run: $total_del trees (${del_mb}M) would be deleted; plan=$PLAN"
  exit 0
fi

freed_mb=0
while IFS=$'\t' read -r verdict mb mtime path reason; do
  [ "$verdict" = "DELETE" ] || continue
  # just-in-time re-verification (fail-safe: any doubt => skip)
  if [ ! -d "$path" ]; then log "GONE     $path"; continue; fi
  if [ -L "$path" ]; then log "SKIP-JIT symlink  $path"; continue; fi
  now_mtime=$(stat -c %Y "$path" 2>/dev/null || echo 0)
  if [ "$now_mtime" != "$mtime" ]; then log "SKIP-JIT mtime-changed $path ($mtime -> $now_mtime)"; continue; fi
  lsof_err="${LOG%.log}.lsof.err"
  lsof_out=$(timeout 120 lsof -w +D "$path" 2>"$lsof_err"); lsof_rc=$?
  if [ "$lsof_rc" = 124 ]; then log "SKIP-JIT lsof-timeout $path"; continue; fi
  if [ "$lsof_rc" -gt 1 ] && [ -z "$lsof_out" ]; then log "SKIP-JIT lsof-rc=$lsof_rc $path"; continue; fi
  if [ -s "$lsof_err" ] && grep -qv 'WARNING' "$lsof_err"; then
    log "SKIP-JIT lsof-stderr $path: $(grep -m1 -v 'WARNING' "$lsof_err")"; continue
  fi
  # holders := non-empty, non-header lines.  See count_holders() above: the
  # naive `grep -vc COMMAND` counted empty output as one holder and made every
  # JIT re-check veto its own deletion (fixed 2026-09-13, guarded by --self-test).
  holders=$(printf '%s\n' "$lsof_out" | count_holders)
  if [ "${holders:-0}" -gt 0 ]; then log "SKIP-JIT open-fds($holders) $path"; continue; fi
  if rm -rf -- "$path" 2>>"$LOG"; then
    if [ -e "$path" ]; then
      log "FAILED   ${mb}M $path (still exists)"
      FAILED=$((FAILED+1))
    else
      log "DELETED  ${mb}M $path ($reason)"
      freed_mb=$((freed_mb+mb))
    fi
  else
    log "FAILED   ${mb}M $path"
    FAILED=$((FAILED+1))
  fi
done <"$PLAN"

log "reclaimed ~${freed_mb}M (du-accounted); failed=${FAILED}"
log "AFTER df -h: $(df1)"
log "AFTER df -B1: $(df -B1 / | tail -1)"
log "=== done (execute) ==="

if [ "$FAILED" -gt 0 ]; then
  printf 'WORKTREE-REAPER FAILURE: %d deletion(s) failed (reclaimed ~%dM) — see %s\n' "$FAILED" "$freed_mb" "$LOG"
  exit 1
fi
say "reaped ~${freed_mb}M; log=$LOG"
exit 0
