#!/usr/bin/env bash
# fleet-store-backup.sh — back up critical CobradorWave data into the encrypted
# restic repo, then replicate to the two mirror repos (x280, DQ05 external).
# Run on CobradorWave. Repo password lives in ~/.hermes/keys/fleet-store/.
set -u
KEYDIR="$HOME/.hermes/keys/fleet-store"
export RESTIC_PASSWORD_FILE="$KEYDIR/restic.password"
export RESTIC_FROM_PASSWORD_FILE="$KEYDIR/restic.password"
PRIMARY="sftp:t440-store:/data/fleet-store/restic"
REPLICAS="sftp:x280-store:/data/fleet-store/restic"
LOG="$HOME/.hermes/profiles/manager/logs/fleet-store-backup.log"
mkdir -p "$(dirname "$LOG")"
log() { printf '[%s] %s\n' "$(date -Is)" "$*" >>"$LOG"; }

log "run start"
if restic -r "$PRIMARY" backup \
      "$HOME/.hermes/keys" \
      "$HOME/.hermes/config.yaml" \
      "$HOME/.hermes/cron" \
      --host cobrador --tag fleet-critical --exclude-caches --quiet 2>>"$LOG"; then
  log "backup ok"
else
  log "backup FAILED"; exit 1
fi

restic -r "$PRIMARY" forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>>"$LOG"
for R in $REPLICAS; do
  if restic -r "$R" copy --from-repo "$PRIMARY" 2>>"$LOG"; then
    restic -r "$R" forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>>"$LOG"
    log "copy+prune ok -> $R"
  else
    log "copy FAILED -> $R"
  fi
done
log "run done"
