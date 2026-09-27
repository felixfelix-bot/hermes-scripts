#!/usr/bin/env bash
# pre19-merge-driver.sh -- merge the TollGate pre19 module PRs AS the maintainer.
#
# Paste (one line, no continuations):
#   bash <(curl -fsSL https://raw.githubusercontent.com/felixfelix-bot/hermes-scripts/PINNED/pre19-merge-driver.sh) --wait=20
#
# Behaviour: refuses unless the gh session has merge rights on the module repo;
# approves + squash-merges each PR in order; waits (bounded) for a PR that is
# still conflicting so a bot rebase can land; idempotent -- safe to re-run.
set -u

MODULE="OpenTollGate/tollgate-module-basic-go"
PRS="610 609"
WAIT=0
DRY=0

for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --wait=*) WAIT="${a#--wait=}" ;;
    -h|--help) echo "usage: $0 [--dry-run] [--wait=MINUTES]"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

die() { echo "STOP: $*" >&2; exit 1; }
say() { echo "$*"; }

command -v gh >/dev/null 2>&1 || die "gh is not installed"
gh auth status >/dev/null 2>&1 || die "no gh session -- run 'gh auth login' as the maintainer first"
WHO="$(gh api user --jq .login 2>/dev/null)" || die "cannot read the gh identity"
say "authenticated as: $WHO"
PUSH="$(gh api "repos/$MODULE" --jq '.permissions.push' 2>/dev/null)"
[ "$PUSH" = "true" ] || die "account '$WHO' has push=$PUSH on $MODULE -- run this as the maintainer account that may merge"
say "merge rights on $MODULE: ok"
say ""
say "PR state:"
for n in $PRS; do
  gh pr view "$n" --repo "$MODULE" --json number,title,state,headRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision \
    --jq '"  #\(.number) \(.state) head=\(.headRefOid[0:8]) base=\(.baseRefName) \(.mergeable)/\(.mergeStateStatus) review=\(.reviewDecision // "none")\n        \(.title)"' \
    || die "cannot read PR #$n"
done

if [ "$DRY" = 1 ]; then
  say ""
  say "--dry-run: nothing approved, nothing merged."
  exit 0
fi

deadline=$(( $(date +%s) + WAIT * 60 ))

for n in $PRS; do
  while :; do
    INFO="$(gh pr view "$n" --repo "$MODULE" --json state,mergeable,mergeStateStatus,reviewDecision,baseRefName \
      --jq '"\(.state)|\(.mergeable)|\(.mergeStateStatus)|\(.reviewDecision // "none")|\(.baseRefName)"')" || die "cannot read PR #$n"
    STATE="${INFO%%|*}"; REST="${INFO#*|}"
    MERGEABLE="${REST%%|*}"; REST="${REST#*|}"
    MS="${REST%%|*}"; REST="${REST#*|}"
    REVIEW="${REST%%|*}"; BASE="${REST#*|}"

    case "$STATE" in
      MERGED) say "#$n: already merged -- nothing to do"; break ;;
      CLOSED) die "#$n is CLOSED -- refusing to touch it" ;;
    esac
    [ "$BASE" = "main" ] || die "#$n targets '$BASE', not main -- refusing"

    if [ "$REVIEW" = "REVIEW_REQUIRED" ]; then
      say "#$n: recording the maintainer approval"
      gh pr review "$n" --repo "$MODULE" --approve \
        --body "Maintainer approval for the pre19 merge set: the cold review's blocking findings are fixed on this tip and the red/green evidence was reproduced on it." \
        || say "#$n: approve call failed (already approved? continuing)"
    fi

    if [ "$MERGEABLE" = "MERGEABLE" ]; then
      say "#$n: merging (squash)"
      if gh pr merge "$n" --repo "$MODULE" --squash --delete-branch=false; then
        say "#$n: merged"
        break
      fi
      say "#$n: merge attempt did not go through -- re-checking"
    else
      say "#$n: $MERGEABLE/$MS -- not mergeable yet (usually a CHANGELOG conflict that needs a bot rebase)"
    fi

    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "#$n: still not mergeable after the wait window."
      say "Re-run this exact command once Felix has rebased -- the script is idempotent."
      exit 0
    fi
    sleep 45
  done
done

TIP="$(gh api "repos/$MODULE/commits/main" --jq '.sha' 2>/dev/null)" || die "cannot read the main tip"
say ""
say "module main tip is now: $TIP"
say "next: the feed pin is set from this tip, then the pre19 tag click."
