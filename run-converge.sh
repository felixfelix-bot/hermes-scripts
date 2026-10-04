#!/bin/bash
# run-converge.sh — re-apply the canonical live-router config (role 29) on the
# fleet, from origin/master, WITHOUT trusting the controller checkout's state.
#
# Why: the canonical checkout (~/hermes-orchestration) once diverged with
# pre-PR copies and a converge clobbered the router fix. This script therefore
# runs the playbook from a throwaway detached worktree at origin/master, so the
# deployed code is always the merged code. It is idempotent: when nothing
# changed, `copy` does not fire and the proxy is not restarted.
#
# Intended for a */6h cron on the controller node. Never fails the cron.
set -uo pipefail
REPO="${HERMES_CONVERGE_REPO:-$HOME/hermes-orchestration}"
LOG="${HERMES_CONVERGE_LOG:-$HOME/.hermes/logs/hermes-converge.log}"
ALERT="$HOME/.hermes/scripts/operator_alert.py"
mkdir -p "$(dirname "$LOG")"

alert() {
    echo "[converge] $*" >> "$LOG"
    if [ -x "$ALERT" ]; then
        python3 "$ALERT" --topic hermes-converge --cooldown 21600 \
            --text "hermes-converge: $*" >> "$LOG" 2>&1
    fi
}

[ -d "$REPO/.git" ] || { echo "[converge] no repo at $REPO" >> "$LOG"; exit 0; }
git -C "$REPO" fetch origin -q 2>/dev/null || true
git -C "$REPO" rev-parse -q --verify origin/master >/dev/null 2>&1 \
    || { alert "cannot resolve origin/master"; exit 0; }

WT="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; }
trap cleanup EXIT

if ! git -C "$REPO" worktree add --detach "$WT" origin/master -q 2>>"$LOG"; then
    alert "could not create worktree at origin/master"
    exit 0
fi

SHA="$(git -C "$WT" rev-parse --short HEAD)"
echo "[converge] $(date -Is) playbook-live-router.yml @ $SHA" >> "$LOG"
( cd "$WT" && ANSIBLE_ROLES_PATH="$WT/roles" \
    ansible-playbook -i inventory.ini playbook-live-router.yml \
    -e repo_dir="$WT" >> "$LOG" 2>&1 )
rc=$?
echo "[converge] $(date -Is) rc=$rc sha=$SHA" >> "$LOG"
[ "$rc" -eq 0 ] || alert "playbook-live-router.yml failed rc=$rc sha=$SHA"
exit 0
