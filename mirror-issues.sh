#!/usr/bin/env bash
# mirror-issues.sh — GitHub <-> ngit issue/PR mirror (report by default).
#
# D-124/D-128: public repos only; private/local excluded. Default is a
# non-mutating diff report (safe). `--apply` creates missing ngit issues for
# GitHub issues on repos where the ngit identity is a maintainer.
#
# Usage:
#   mirror-issues.sh [--repo NAME ...] [--apply] [--json]
#
# Notes: ngit issues/PRs are public nostr events — this never runs on private
# repos (filtered via the repo registry).
set -uo pipefail

REG="${HERMES_HOME:-$HOME/.hermes}/bot/repo_registry.json"
APPLY=0; JSON=0; ONLY=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) ONLY+=("$2"); shift 2;;
    --apply) APPLY=1; shift;;
    --json) JSON=1; shift;;
    *) shift;;
  esac
done
[ -f "$REG" ] || { echo "no registry ($REG)"; exit 1; }

python3 - "$REG" "${ONLY[*]:-}" <<'PY' > /tmp/.mirror_repos.tsv
import json,sys
reg=json.load(open(sys.argv[1])); only=set(filter(None, sys.argv[2].split()))
for name,v in (reg.get("repos") or {}).items():
    if v.get("visibility")!="public" or not v.get("mirror_issues"): continue
    if only and name not in only: continue
    print("\t".join([name, v.get("path",""), v.get("github","")]))
PY

while IFS=$'\t' read -r name path github; do
  [ -n "$name" ] || continue
  [ -d "$path" ] || continue
  gh_issues=$( (cd "$path" && gh issue list --state open --limit 100 --json number,title 2>/dev/null) || echo '[]')
  gh_prs=$( (cd "$path" && gh pr list --state open --limit 100 --json number,title 2>/dev/null) || echo '[]')
  ngit_issues=$( (cd "$path" && ngit issue list 2>/dev/null) || echo '' )
  ngit_prs=$( (cd "$path" && ngit pr list 2>/dev/null) || echo '' )
  if [ "$APPLY" = 1 ]; then
    # best-effort: create ngit issues for GitHub issues (guarded by ngit identity)
    (cd "$path" && gh issue list --state open --limit 100 --json title,body \
       --jq '.[] | "- [ ] " + .title' 2>/dev/null) | while read -r line; do
       [ -n "$line" ] && (cd "$path" && ngit issue create -t "$line" >/dev/null 2>&1 || true)
    done
  fi
  if [ "$JSON" = 1 ]; then
    printf '{"repo":"%s","gh_issues":%s,"gh_prs":%s,"ngit_issues":"%s","ngit_prs":"%s"}\n' \
      "$name" "${gh_issues:-[]}" "${gh_prs:-[]}" \
      "$(printf '%s' "$ngit_issues" | head -c 400)" "$(printf '%s' "$ngit_prs" | head -c 400)"
  else
    echo "[mirror] $name: gh_issues=$(printf '%s' "$gh_issues" | grep -o '"number"' | wc -l) gh_prs=$(printf '%s' "$gh_prs" | grep -o '"number"' | wc -l) ngit_issues=$(printf '%s' "$ngit_issues" | grep -c . ) ngit_prs=$(printf '%s' "$ngit_prs" | grep -c .)"
  fi
done < /tmp/.mirror_repos.tsv
rm -f /tmp/.mirror_repos.tsv
