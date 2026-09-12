#!/usr/bin/env bash
# gh_cli_healthcheck.sh - regression guard for the gh "projectCards" breakage.
#
# WHY: gh < 2.60 fetches a PR through a GraphQL query asking for the retired
# Projects (classic) `projectCards` field. GitHub answers NOT_FOUND, gh prints
# "GraphQL: Projects (classic) is being deprecated ... (repository.pullRequest.projectCards)"
# and ABORTS before mutating. Some versions/callers treated that as success
# (observed exit 0 in the fleet) => phantom-success `gh pr edit`, e.g. a
# "review requested" claim that never landed. Fixed by gh >= 2.60.
#
# CobradorWave upgraded 2026-09-13: gh 2.46.0-4 (Ubuntu) -> 2.100.0 (official .deb).
#
# WHAT THIS DOES: checks the installed version, then performs a REAL mutating
# `gh pr edit` on a scratch PR and asserts the change landed by reading the
# state back over REST (REST is unaffected by the GraphQL bug). Restores state.
# Never trust `gh pr edit` exit status alone - that is the whole point.
#
# USAGE: gh_cli_healthcheck.sh [OWNER/REPO PR_NUMBER LABEL]
#        defaults: felixfelix-bot/market 14 bug
# EXIT:   0 = healthy, 1 = broken (version too old / mutation silently ignored)

set -uo pipefail

MIN_MINOR=60            # first gh release line where projectCards left the PR query
REPO="${1:-felixfelix-bot/market}"
PR="${2:-14}"
LABEL="${3:-bug}"

fail() { echo "FAIL: $*" >&2; exit 1; }

if ! command -v gh >/dev/null 2>&1; then
  fail "gh not found on PATH"
fi

# --- 1. version gate -------------------------------------------------------
RAW_VER="$(gh --version 2>&1 | head -1)"
VER="$(printf '%s\n' "$RAW_VER" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ -n "$VER" ] || fail "cannot parse gh version from: $RAW_VER"
MAJ="${VER%%.*}"; REST="${VER#*.}"; MIN="${REST%%.*}"
echo "gh version: $VER ($RAW_VER)"
if [ "$MAJ" -lt 2 ] || { [ "$MAJ" -eq 2 ] && [ "$MIN" -lt "$MIN_MINOR" ]; }; then
  fail "gh $VER < 2.$MIN_MINOR - every 'gh pr edit' mutation aborts on projectCards. Upgrade gh."
fi

# --- 2. real mutating edit, verified by REST readback ----------------------
if ! labels_before="$(gh api "repos/$REPO/issues/$PR" --jq '[.labels[].name]|join(",")' 2>/dev/null)"; then
  fail "cannot read labels of $REPO#$PR over REST (repo/PR wrong, or no auth)"
fi
echo "labels before: [${labels_before:-(none)}]"

if ! gh pr edit "$PR" --repo "$REPO" --add-label "$LABEL" >/dev/null 2>&1; then
  fail "'gh pr edit --add-label $LABEL' on $REPO#$PR returned non-zero (this is the honest failure mode)"
fi

labels_after="$(gh api "repos/$REPO/issues/$PR" --jq '[.labels[].name]|join(",")' 2>/dev/null)"
echo "labels after : [${labels_after}]"
case ",$labels_after," in
  *",$LABEL,"*) echo "OK: mutation landed (verified by REST readback)" ;;
  *) fail "gh pr edit exited 0 but '$LABEL' is NOT on $REPO#$PR - phantom-success mutation" ;;
esac

# --- 3. restore prior state -------------------------------------------------
case ",$labels_before," in
  *",$LABEL,"*) echo "label was already present before the test - left as-is" ;;
  *)  if ! gh pr edit "$PR" --repo "$REPO" --remove-label "$LABEL" >/dev/null 2>&1; then
        echo "WARN: could not remove test label '$LABEL' from $REPO#$PR - remove it manually" >&2
      fi
      now="$(gh api "repos/$REPO/issues/$PR" --jq '[.labels[].name]|join(",")' 2>/dev/null)"
      echo "restored  : [${now:-(none)}]"
      case ",$now," in
        *",$LABEL,"*) fail "test label '$LABEL' still present after removal - state not restored" ;;
      esac ;;
esac

echo "PASS: gh $VER mutates PR metadata for real on $(hostname)"
exit 0
