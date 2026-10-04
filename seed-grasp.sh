#!/usr/bin/env bash
# seed-grasp.sh — seed the 2,198-repo grasp-git-repos tree as PER-REPO entries
# (the giant tree is impractical as one entry on dq05's slow disk). Relays
# through this host; each repo is a small, independently-verified entry.
set -u
log="$HOME/.hermes/profiles/manager/logs/relay-seed.log"
base="/home/c03rad0r/backups/orangesync/grasp-git-repos"
echo "=== seed-grasp start $(date -Is) ===" >>"$log"
repos=$(ssh -o BatchMode=yes dq05 "ls -1d $base/* 2>/dev/null")
n=0; ok=0
for r in $repos; do
  if "$HOME/.hermes/scripts/relay-seed-fast.sh" "$r" >>"$log" 2>&1; then ok=$((ok+1)); else echo "FAILED $r" >>"$log"; fi
  n=$((n+1))
  [ $((n % 50)) -eq 0 ] && echo "progress: $n repos ($ok ok) $(date -Is)" >>"$log"
done
echo "=== seed-grasp done: $n repos, $ok ok $(date -Is) ===" >>"$log"
