#!/usr/bin/env bash
# seed-dq05.sh — seed dq05 cold data into t440+x280 as per-subdir entries,
# relaying through this host. Idempotent (skips sources already in the index),
# skips the regenerable grasp-git-repos tree, and is safe to restart.
# Sends a Signal completion notification via fleet_notify.py (best-effort).
set -u
log="$HOME/.hermes/profiles/manager/logs/relay-seed.log"
repo="$HOME/repos/fleet-store-index"
frag="$repo/nodes/dq05.jsonl"
NOTIFY="$HOME/.hermes/scripts/fleet_notify.py"
echo "=== seed-dq05 start $(date -Is) ===" >>"$log"
paths=$(ssh -o BatchMode=yes dq05 'ls -1d /home/c03rad0r/backups/orangesync/* /home/c03rad0r/backups/cobradorwave/* 2>/dev/null')
ok=0; fail=0; skip=0
for d in $paths; do
  case "$d" in
    *grasp-git-repos) echo "SKIP(regenerable) $d" >>"$log"; skip=$((skip+1)); continue;;
    *cobradorwave/archive|*ngit-relay-exports|*strfry-relay-exports)
      seeder="$HOME/.hermes/scripts/relay-seed-rsync.sh";;
    *) seeder="$HOME/.hermes/scripts/relay-seed-fast.sh";;
  esac
  if grep -qF "\"source\": \"$d\"" "$frag" 2>/dev/null; then
    echo "SKIP(indexed) $d" >>"$log"; skip=$((skip+1)); continue
  fi
  echo "== $d $(date -Is) ($(basename "$seeder")) ==" >>"$log"
  if "$seeder" "$d" >>"$log" 2>&1; then ok=$((ok+1)); else echo "FAILED $d" >>"$log"; fail=$((fail+1)); fi
done
msg="cold-store seed (dq05) done $(date -Is): seeded=$ok skipped=$skip failed=$fail (targets t440+x280)"
echo "=== seed-dq05 done: $msg ===" >>"$log"
[ -f "$NOTIFY" ] && python3 "$NOTIFY" "$msg" >/dev/null 2>&1 || true
