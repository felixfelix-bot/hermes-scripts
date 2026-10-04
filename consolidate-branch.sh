#!/usr/bin/env bash
# consolidate-branch.sh — never lose work: merge to default (owner) or PR (fork).
#
# D-116 + D-124 + D-128. For a repo with uncommitted/feature work:
#   * sole-maintainer repo  -> commit, merge the feature branch into the default
#                              branch, push (public: GitHub+ngit; private: GitHub)
#   * third-party/fork repo -> push the feature branch to the fork remote and
#                              ensure an upstream PR exists (D-116 single PR)
#
# A pre-merge backup tag is always created: backup/pre-consolidate-<ts>.
#
# Usage: consolidate-branch.sh <repo> [branch] [--push] [--dry-run]
set -uo pipefail

REPO="${1:?usage: consolidate-branch.sh <repo> [branch] [--push] [--dry-run]}"
BRANCH="${2:-}"
MODE="${3:-}"
[ -d "$REPO/.git" ] || { echo "not a git repo: $REPO"; exit 2; }
cd "$REPO"

our_owners="felixfelix-bot c03rad0r"
origin_url=$(git remote get-url origin 2>/dev/null || echo "")
owner=$(echo "$origin_url" | sed -nE 's#.*github\.com[:/]+([^/]+)/.*#\1#p')
if echo " $our_owners " | grep -q " $owner "; then role=owner; else role=fork; fi

def=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#origin/##')
def=${def:-$(git show-ref --verify --quiet refs/heads/main && echo main || echo master)}
cur=$(git rev-parse --abbrev-ref HEAD)
do_push=false; [ "$MODE" = "--push" ] && do_push=true
dry=false; [ "$MODE" = "--dry-run" ] && dry=true

echo "[consolidate] $REPO role=$role default=$def current=$cur"

if [ -n "$(git status --porcelain)" ]; then
  echo "[consolidate] committing working tree"
  $dry || { git add -A && git commit -q -m "chore: consolidate working tree (D-128)" || true; }
fi

tag="backup/pre-consolidate-$(date +%Y%m%d-%H%M%S)"
$dry || git tag -f "$tag" >/dev/null 2>&1 || true
echo "[consolidate] backup tag: $tag"

if [ "$cur" != "$def" ]; then
  if [ "$role" = owner ]; then
    echo "[consolidate] merging $cur -> $def"
    if ! $dry; then
      git checkout -q "$def" && git merge --no-ff -m "merge: $cur into $def (D-128)" "$cur" && git checkout -q "$cur"
    fi
  else
    # Fork/PR: PR branches MUST use the `pr/` prefix on BOTH remotes so ngit
    # resolves them as PRs (D-135). GitHub accepts `pr/<branch>` too.
    case "$cur" in pr/*) pr_branch="$cur";; *) pr_branch="pr/${cur}";; esac
    echo "[consolidate] fork: push $cur -> $pr_branch (origin + ngit) and ensure PR"
    if ! $dry; then
      git push -q -u origin "${cur}:${pr_branch}" 2>/dev/null || true
      if git remote | grep -qx ngit; then
        git push -q --no-verify ngit "${cur}:${pr_branch}" 2>/dev/null || true
      fi
      gh pr view "$pr_branch" >/dev/null 2>&1 || \
        gh pr create --fill --draft --head "$pr_branch" 2>/dev/null || true
    fi
  fi
fi

if $do_push && ! $dry; then
  bash "$(dirname "$0")/git-push-all.sh" --repo "$REPO" || true
fi
echo "[consolidate] done: $REPO"
