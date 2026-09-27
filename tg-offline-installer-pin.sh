#!/usr/bin/env bash
# =============================================================================
#  TollGate — point the offline bundle at the FIXED upstream installer
#
#  WHY THIS IS A MANUAL STEP: the offline bundle is built from a PINNED copy of
#  OpenTollGate/physical-router-test-automation (scripts/offline/), selected by a
#  repository variable. Setting a repository variable needs repo admin, which the
#  automation account does not have — so a maintainer runs this.
#
#  WHAT IT CHANGES: OFFLINE_INSTALLER_REF on FreedomTechFeed/packages, from the
#  pre-fix commit to the merge commit of PR #178, which makes install-router.sh
#  stage (2) offer the WHOLE staged closure to apk in one --no-network
#  transaction and report apk's own rc (a fresh box previously got
#  "REFUSED(7): the offline dependency install failed" with a bundle that
#  carried every package it needed).
#
#  Run as the maintainer (c03rad0r):
#      bash <(curl -fsSL https://raw.githubusercontent.com/felixfelix-bot/hermes-scripts/master/tg-offline-installer-pin.sh)
#  Check only, change nothing:
#      ... | bash -s -- --check
#
#  Idempotent: safe to run twice. Needs `gh` authenticated as c03rad0r.
# =============================================================================
set -euo pipefail

REPO="FreedomTechFeed/packages"
SRC_REPO="OpenTollGate/physical-router-test-automation"
OLD_PIN="ff445e4de47439040220e04ee20854ce0fb4ffcd"
NEW_PIN="dc37d1b259570fb6824e0d4af86554ab604c5958"
VAR="OFFLINE_INSTALLER_REF"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"

ME="$(gh api user --jq .login 2>/dev/null || true)"
say "gh account: ${ME:-<unknown>}"
if [ "${ME}" != "c03rad0r" ]; then
  say "NOTE: this needs repo ADMIN on ${REPO}; the automation account does not have it."
  say "      Proceeding will likely fail with a 403 — that is expected, not a bug."
fi

CUR="$(gh variable list --repo "${REPO}" --json name,value \
        --jq ".[] | select(.name==\"${VAR}\") | .value" 2>/dev/null || true)"
say "current ${VAR}: ${CUR:-<unset>}"

if [ "${CUR}" = "${NEW_PIN}" ]; then
  say "already pinned to the fixed commit — nothing to do."
else
  if [ "${CHECK_ONLY}" = 1 ]; then
    say "--check: would set ${VAR} = ${NEW_PIN} (was ${CUR:-<unset>})"
  else
    gh variable set "${VAR}" --repo "${REPO}" --body "${NEW_PIN}"
    NOW="$(gh variable list --repo "${REPO}" --json name,value \
            --jq ".[] | select(.name==\"${VAR}\") | .value" 2>/dev/null || true)"
    [ "${NOW}" = "${NEW_PIN}" ] || die "set did not take (now: ${NOW:-<unset>})"
    say "set ${VAR} = ${NOW}"
  fi
fi

say ""
say "verifying that the pinned source really is the fixed one:"
TMP="$(mktemp)"; trap 'rm -f "${TMP}"' EXIT
if gh api "repos/${SRC_REPO}/contents/scripts/offline/install-router.sh?ref=${NEW_PIN}" \
     --jq .content 2>/dev/null | base64 -d > "${TMP}" 2>/dev/null; then
  say "  pinned file fetched ($(wc -l < "${TMP}") lines)"
  grep -q 'full-closure offer' "${TMP}" \
    && say "  OK: whole-closure dependency stage present" \
    || say "  WARNING: whole-closure stage NOT found in the pinned file"
  grep -q 'apk_deps_rc' "${TMP}" \
    && say "  OK: gate reports apk's own rc" \
    || say "  WARNING: honest rc reporting NOT found"
  # The defective filter and the legitimate bundle-closure audit are textually
  # similar (both walk REQUIRED_DEPS with a "$dep-"* case pattern), so text
  # matching anywhere in the file cannot tell them apart. Scope the check to the
  # dependency-INSTALL stage itself: from the "(2) dependency packages" banner to
  # its gate_pass line.
  awk '/\(2\) dependency packages/{f=1} f{print} /gate_pass deps_installed/{f=0}' "${TMP}" > "${TMP}.stage2"
  if [ -s "${TMP}.stage2" ]; then
    say "  stage (2) region: $(wc -l < "${TMP}.stage2") lines"
    grep -q 'for f in \$STAGED_APKS' "${TMP}.stage2" \
      && say "  OK: stage (2) iterates the whole staged closure" \
      || say "  WARNING: stage (2) does not iterate the staged closure"
    if grep -q '"\$dep-"\*' "${TMP}.stage2"; then
      say "  WARNING: the OLD named-deps filter is still in stage (2) -- pin may be wrong"
    else
      say "  OK: the old named-deps filter is gone from stage (2)"
    fi
  else
    say "  WARNING: could not locate stage (2) in the pinned file -- inspect by hand"
  fi
else
  say "  could not fetch the file at that ref (network or permission) — verify by hand"
fi

say ""
say "done. The fixed source is in place; the next offline bundle build picks it up."
say "NOTE: the feed builder also applies fresh-box repairs (PRs #36/#37) to the staged"
say "      copy. With the source fixed those repairs should no-op — if a bundle build"
say "      fails closed instead, that is a follow-up, not a reason to re-pin."
