#!/usr/bin/env bash
# repo_drift_cron.sh — refresh the clean deploy worktree and audit drift (L2).
# Gateway-independent (systemd timer). Alerts only on NEW authority=repo drift.
#
# DANGER: this hard-resets $HERMES_DEPLOY_WT. That worktree MUST be a dedicated
# deploy worktree, never an active editing checkout.
set -uo pipefail

WT="${HERMES_DEPLOY_WT:-$HOME/worktrees/ho-deploy}"
REPO="${HERMES_ORCH_REPO:-$HOME/hermes-orchestration}"
BOT="${HERMES_HOME:-$HOME/.hermes}/bot"
SCRIPTS="${HERMES_HOME:-$HOME/.hermes}/scripts"
BASE="${HERMES_DRIFT_BASE:-origin/master}"

# Safety: refuse if the deploy worktree is the main checkout.
MAIN_TOP="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || true)"
WT_TOP="$(git -C "$WT" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$WT_TOP" ] && [ "$WT_TOP" = "$MAIN_TOP" ]; then
  echo "repo_drift_cron: refusing to reset the main checkout ($WT)" >&2
  exit 2
fi

# Refresh the dedicated worktree to the canonical base.
if [ -n "$WT_TOP" ]; then
  git -C "$REPO" fetch --quiet origin 2>/dev/null || true
  git -C "$WT" fetch --quiet origin 2>/dev/null || true
  git -C "$WT" reset --hard --quiet "$BASE" 2>/dev/null || true
  git -C "$WT" clean -fdq 2>/dev/null || true
  ROOT="$WT"; REPO_ARG="$WT"; WT_ARG="$WT"
else
  ROOT="$REPO"; REPO_ARG="$REPO"; WT_ARG=""
fi

ARGS=(--repo "$REPO_ARG" --root "$ROOT" --base-ref "$BASE"
      --manifest "$BOT/managed_files.json"
      --ledger "$BOT/repo_drift_ledger.jsonl"
      --state "$BOT/repo_drift_state.json"
      --quarantine --quarantine-dir "${HERMES_HOME:-$HOME/.hermes}/quarantine")
[ -n "$WT_ARG" ] && ARGS+=(--worktree "$WT_ARG")

python3 "$SCRIPTS/repo_drift_check.py" "${ARGS[@]}"
