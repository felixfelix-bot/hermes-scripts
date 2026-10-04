#!/usr/bin/env bash
# make-pr-branch.sh — push a branch as `pr/<branch>` to origin AND ngit (D-134).
#
# ngit resolves branches prefixed `pr/` as PRs, so every PR branch MUST use the
# prefix on both remotes. Usage: make-pr-branch.sh <repo> <branch> [--dry-run]
set -uo pipefail
REPO="${1:?usage: make-pr-branch.sh <repo> <branch> [--dry-run]}"
BRANCH="${2:?usage: make-pr-branch.sh <repo> <branch> [--dry-run]}"
DRY=0; [ "${3:-}" = "--dry-run" ] && DRY=1
git -C "$REPO" rev-parse --verify "$BRANCH" >/dev/null 2>&1 || { echo "no such branch: $BRANCH"; exit 2; }
case "$BRANCH" in pr/*) PR="$BRANCH";; *) PR="pr/$BRANCH";; esac
echo "[pr] $REPO: $BRANCH -> $PR"
if [ "$DRY" = 1 ]; then
  echo "[pr] dry-run: would push origin ${BRANCH}:${PR}" \
       "$([ "$(git -C "$REPO" remote | grep -cx ngit)" = 1 ] && echo "+ ngit ${BRANCH}:${PR}")"
  exit 0
fi
git -C "$REPO" push -u origin "${BRANCH}:${PR}"
if git -C "$REPO" remote | grep -qx ngit; then
  git -C "$REPO" push --no-verify ngit "${BRANCH}:${PR}"
fi
echo "[pr] pushed $PR"
