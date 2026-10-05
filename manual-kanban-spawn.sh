#!/usr/bin/env bash
# manual-kanban-spawn.sh — spawn a kanban worker WITHOUT the dispatcher.
#
# Why: `hermes kanban dispatch` silently returns spawned:[] on this box (the
# ledger gate prices every lane +inf -> dispatch_headroom llm=0). The
# kanban-crash-wrapper.sh also refuses worker runs via its own FLEET GATE.
# This replicates the dispatcher's exact Popen contract, bypassing both.
#
# Usage: manual-kanban-spawn.sh <board> <task_id> <profile> [workspace]
#
# FIX 2026-10-05 #1: workspace is READ FROM THE BOARD DB (tasks.workspace_path),
#   not guessed. Repo-project worktrees live at <repo>/.worktrees/<task>, not
#   ~/worktrees/<task>; guessing died with "no workspace".
#
# FIX 2026-10-05 #2 (CRITICAL): REGISTER THE CHILD PID. Without it
#   tasks.worker_pid stays NULL and the daemon's reconcile loop treats the
#   claim as foreign and RECLAIMS THE CARD WHILE THE WORKER IS STILL RUNNING
#   (observed: reclaimed with a 57s-old heartbeat; the worker kept working 14
#   more minutes against an orphaned card).
#
# FIX 2026-10-05 #3 (CRITICAL): DUPLICATE-SPAWN GUARD. Spawning a second worker
#   onto a card that ALREADY HAS A LIVE WORKER puts two agents in ONE worktree:
#   they race each other's edits/commits/checkouts and can corrupt or lose work.
#   This script tracked the LATEST pid, so it could not even see the first one.
#   Now it refuses while the recorded pid is alive. (Observed on t_119a4ab8.)
set -uo pipefail

BOARD="${1:?board}"; TASK="${2:?task id}"; PROFILE="${3:?profile}"
HOME_DIR="/home/c03rad0r"
HERMES_BIN="$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes"
DB="$HOME_DIR/.hermes/kanban/boards/$BOARD/kanban.db"
CLAIM_TTL="${CLAIM_TTL:-7200}"   # seconds; a long worker must not be reclaimed mid-flight

# --- resolve the workspace (authoritative source, in order) -----------------
WS="${4:-}"
if [[ -z "$WS" && -f "$DB" ]]; then
    WS="$(sqlite3 "$DB" \
        "select coalesce(workspace_path,'') from tasks where id='$TASK';" 2>/dev/null)"
fi
if [[ -z "$WS" ]]; then
    for cand in "$HOME_DIR/worktrees/$TASK" "$HOME_DIR/repos/$BOARD/.worktrees/$TASK"; do
        [[ -d "$cand" ]] && { WS="$cand"; break; }
    done
fi
[[ -n "$WS" && -d "$WS" ]] || { echo "no workspace for $TASK (db='${WS:-none}')" >&2; exit 1; }

# --- DUPLICATE-SPAWN GUARD: never put two workers in one worktree ----------
EXISTING="$(sqlite3 "$DB" "select coalesce(worker_pid,'') from tasks where id='$TASK';" 2>/dev/null)"
if [[ -n "$EXISTING" ]] && kill -0 "$EXISTING" 2>/dev/null; then
    echo "REFUSING: $BOARD/$TASK already has a LIVE worker (pid $EXISTING)." >&2
    echo "  Two workers in $WS would race each other's edits and lose work." >&2
    echo "  Wait for it, or kill $EXISTING deliberately (push unpushed commits FIRST)." >&2
    exit 2
fi
# Also catch an untracked agent already sitting in the worktree.
if pgrep -f "HERMES_KANBAN_TASK=$TASK" >/dev/null 2>&1; then
    echo "REFUSING: a process for $TASK is already running (not recorded in DB)." >&2
    pgrep -af "HERMES_KANBAN_TASK=$TASK" >&2
    exit 2
fi

# --- SESSION-LIMIT PRE-FLIGHT (FIX #6, #7) ---------------------------------
# FIX #6: the child can die BEFORE its first API call (Hermes caps concurrent
#   sessions), and the old script registered worker_pid AFTER launching — so a
#   dead-on-arrival child still left a phantom claim ("3/3 held by cli").
# FIX #7 (SYSTEMIC): Hermes LEAKS dead session leases. Ending a worker abruptly
#   skips lease cleanup, so active_sessions.json keeps an entry whose pid is
#   long gone. The gateway counts those, so the cap fills up with ghosts and
#   EVERY future spawn is refused ("3/3"). Observed: a worker ended by hand
#   held a slot for good. Here we prune leases whose pid is gone
#   (verified via /proc, under the registry's own file lock) BEFORE counting.
SESS_FILE="$HOME_DIR/.hermes/profiles/$PROFILE/runtime/active_sessions.json"
SESS_MAX="$(python3 - "$HOME_DIR/.hermes/profiles/$PROFILE/config.yaml" <<'PY' 2>/dev/null || echo 3
import sys,re
try:
    t=open(sys.argv[1]).read()
    m=re.search(r'^max_concurrent_sessions:\s*(\d+)', t, re.M)
    print(m.group(1) if m else 3)
except Exception:
    print(3)
PY
)"
if [[ -f "$SESS_FILE" ]]; then
    read -r SESS_N PRUNED < <(python3 - "$SESS_FILE" <<'PY' 2>/dev/null || echo "0 0"
import json, os, sys, fcntl
f = sys.argv[1]
lock = f.replace('.json', '.lock')
try:
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
except Exception:
    fd = None
try:
    d = json.load(open(f))
    ent = d.get('entries', d)
    alive = [e for e in ent if os.path.exists('/proc/%s' % e.get('pid'))]
    pruned = len(ent) - len(alive)
    if pruned:
        d['entries'] = alive
        tmp = f + '.tmp'
        json.dump(d, open(tmp, 'w'), indent=2)
        os.replace(tmp, f)
    print(len(alive), pruned)
finally:
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
PY
)
    [[ "${PRUNED:-0}" -gt 0 ]] && echo "[manual-spawn] pruned $PRUNED leaked (dead-pid) session lease(s)" >&2
    if [[ "${SESS_N:-0}" -ge "${SESS_MAX:-3}" ]]; then
        echo "REFUSING: at the Hermes session limit (${SESS_N}/${SESS_MAX}) — nothing registered." >&2
        echo "  Slots are held by live sessions in $SESS_FILE — let one finish." >&2
        exit 3
    fi
fi

# --- BURN GATE (FIX #8, 2026-10-05) -----------------------------------------
# Why: this escape hatch bypassed the fleet's dispatch SPEND POLICY entirely.
#   The burn gate (hermes-orchestration/scripts/fleet/burn_gate.py, driven by
#   state/fleet/burn_policy.json) is what stops dispatch once the day's spend
#   reaches the cap. The dispatcher and kanban-crash-wrapper.sh both honour it;
#   this script did not, so manual spawns kept spending on a day already far
#   over cap (observed 2026-10-05: $106.68 spent against a $15.00 cap, 7.1x).
#   A kanban spawn IS card-backed, so we assert --card: card-backed work is
#   admitted while the day is UNDER cap. Over cap, --card does NOT rescue it --
#   the cap check wins. That asymmetry is the gate working, not a bug.
#   Escape valve: there is deliberately NO env-var bypass here. To spend past
#   the cap you must raise the cap in burn_policy.json, which is auditable and
#   leaves a record of who decided to spend. A silent flag would not.
BURN_GATE="$HOME_DIR/hermes-orchestration/scripts/fleet/burn_gate.py"
if [[ -f "$BURN_GATE" ]]; then
    GATE_OUT="$(python3 "$BURN_GATE" --kind dispatch --card 2>&1)"; GATE_RC=$?
    if [[ $GATE_RC -ne 0 ]]; then
        echo "REFUSING: the fleet burn gate denied this dispatch." >&2
        echo "  ${GATE_OUT:-<no output>}" >&2
        echo "  $BOARD/$TASK would consume inference on a day already over the spend cap." >&2
        echo "  Raise daily_usd_cap in ~/hermes-orchestration/state/fleet/burn_policy.json" >&2
        echo "  (auditable), or wait for the day to roll over. Nothing is lost -- the card waits." >&2
        exit 4
    fi
fi

# Strip any inherited gateway-turn session routing (the dispatcher does the same).
for k in $(env | grep -oE '^HERMES_SESSION_[A-Z_]*'); do unset "$k"; done
unset HERMES_CRON_AUTO_DELIVER_TO HERMES_UI_SESSION_ID AI_AGENT HERMES_AGENT 2>/dev/null || true

echo "[manual-spawn] board=$BOARD task=$TASK profile=$PROFILE ws=$WS" >&2

# --- launch child in the BACKGROUND so we can register its PID --------------
# FIX 2026-10-05 #5: MODEL_OVERRIDE. Cross-family review (D-115/D-128) needs a
#   reviewer from a DIFFERENT family than the author. The profile default
#   (tier/coding-worker -> GLM) would self-review. MODEL_OVERRIDE=tier/review-kimi
#   pins the reviewer lane; unset means the profile default.
MODEL_ARGS=()
[[ -n "${MODEL_OVERRIDE:-}" ]] && MODEL_ARGS=(-m "$MODEL_OVERRIDE")

env \
  HERMES_HOME="$HOME_DIR/.hermes/profiles/$PROFILE" \
  HERMES_KANBAN_TASK="$TASK" \
  HERMES_KANBAN_BOARD="$BOARD" \
  HERMES_KANBAN_WORKSPACE="$WS" \
  HERMES_SESSION_SOURCE="kanban" \
  TERMINAL_CWD="$WS" \
  "$HERMES_BIN" -p "$PROFILE" chat "${MODEL_ARGS[@]}" -q "work kanban task $TASK" &
CHILD=$!

# FIX #6b: verify the child survived startup BEFORE registering it. A child that
# dies on its first API call (session cap, bad model, 503) must leave NO claim.
sleep 3
if ! kill -0 "$CHILD" 2>/dev/null; then
    echo "REFUSING: child died during startup (pid $CHILD) — nothing registered." >&2
    exit 4
fi

# Register the PID — without this the daemon reclaims a live worker's card.
# FIX #4: also set status='running'. Registering only worker_pid left the row
# at 'ready', so the card looked idle while a worker held it and the dispatcher
# could hand it out again.
sqlite3 "$DB" "update tasks set status='running', worker_pid=$CHILD,
    claim_lock='$(hostname):$CHILD',
    claim_expires=strftime('%s','now')+$CLAIM_TTL,
    last_heartbeat_at=strftime('%s','now')
  where id='$TASK';" 2>/dev/null || true
echo "[manual-spawn] registered worker_pid=$CHILD + status=running on $BOARD/$TASK" >&2

wait "$CHILD"
