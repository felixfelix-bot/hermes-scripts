#!/usr/bin/env bash
# git-push-all.sh — registry-driven push (D-124/D-128).
#
# Visibility policy:
#   public  -> GitHub (origin) + ngit
#   private -> GitHub (origin) only
#   local   -> never pushed
#
# Usage:
#   git-push-all.sh                 # all repos in ~/.hermes/bot/repo_registry.json
#   git-push-all.sh --repo PATH     # a single repo (visibility looked up by name)
#   git-push-all.sh --build         # rebuild the registry first
#   git-push-all.sh --commit        # commit dirty trees before pushing
set -uo pipefail

REG="${HERMES_HOME:-$HOME/.hermes}/bot/repo_registry.json"
HERE="$(cd "$(dirname "$0")" && pwd)"
ONLY=""; BUILD=0; COMMIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) ONLY="$2"; shift 2;;
    --build) BUILD=1; shift;;
    --commit) COMMIT=1; shift;;
    *) shift;;
  esac
done

[ "$BUILD" = 1 ] && python3 "$HERE/build_registry.py" || true
[ -f "$REG" ] || { echo "no registry at $REG (run --build)"; exit 1; }

# D-135: --no-verify bypasses the pre-push hook, so the PR-branch naming rule
# is enforced here explicitly. Only PR pushes (ngit/fork) are checked.
guard_ok() {  # $1=repo $2=remote-name
  local g="$HERE/pr-branch-guard.sh"
  [ -x "$g" ] || return 0
  local br; br=$(git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ -n "$br" ] || return 0
  "$g" check "$1" "$2" "$(git -C "$1" remote get-url "$2" 2>/dev/null || echo)" "$br"
}

push_one() {
  local name="$1" path="$2" vis="$3" def="$4" has_ngit="$5"
  [ -d "$path/.git" ] || return 0
  if [ "$COMMIT" = 1 ] && [ -n "$(git -C "$path" status --porcelain)" ]; then
    git -C "$path" add -A && git -C "$path" commit -q -m "chore: end-of-work commit (D-128)" || true
  fi
  local ahead
  ahead=$(git -C "$path" rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0)
  case "$vis" in
    local)
      echo "[push] $name: local-only — skipped"; return 0;;
    public)
      echo "[push] $name: public -> origin + ngit"
      guard_ok "$path" origin && git -C "$path" push --no-verify origin HEAD 2>&1 | tail -1
      if [ "$has_ngit" = "yes" ]; then
        if guard_ok "$path" ngit; then
          git -C "$path" push --no-verify ngit HEAD 2>&1 | tail -1
        else
          echo "[push] $name: ngit skipped (PR-branch naming; use scripts/git/make-pr-branch.sh)"
        fi
      else
        echo "[push] $name: WARN no ngit remote (attach manually; never ngit init on an existing origin)"
      fi;;
    private)
      echo "[push] $name: private -> origin only"
      guard_ok "$path" origin && git -C "$path" push --no-verify origin HEAD 2>&1 | tail -1;;
    *)
      echo "[push] $name: unknown visibility '$vis' — skipped (safe)";;
  esac
}

python3 - "$REG" "$ONLY" <<'PY' > /tmp/.push_all.tsv
import json,sys
reg=json.load(open(sys.argv[1])); only=sys.argv[2]
for name,v in (reg.get("repos") or {}).items():
    if only and v.get("path")!=only and name!=only: continue
    print("\t".join([name, v.get("path",""), v.get("visibility","unknown"),
                     v.get("default_branch","master"),
                     "yes" if v.get("ngit") else "no"]))
PY
while IFS=$'\t' read -r name path vis def has_ngit; do
  [ -n "$name" ] && push_one "$name" "$path" "$vis" "$def" "$has_ngit"
done < /tmp/.push_all.tsv
rm -f /tmp/.push_all.tsv
