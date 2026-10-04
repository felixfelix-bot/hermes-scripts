#!/usr/bin/env bash
# indiamart_restore.sh — reverse indiamart_decommission.sh (recoverable).
#
# Restores the archived IndiaMART files + the crontab from the backups made by
# the decommission script. Chrome/processes resume on the next cron fire (or
# start price-monitor.js manually).
#
# Usage:
#   indiamart_restore.sh [--apply] [--archive PATH] [--crontab PATH]
# Defaults pick the newest /home/c03rad0r/indiamart-archive-*.tar.gz on VPS2.
set -uo pipefail

VPS2="${VPS2_HOST:-debian@23.182.128.51}"
REMOTE_DIR="${INDIAMART_REMOTE_DIR:-/home/c03rad0r/automation}"
APPLY=0
ARCHIVE=""
CRON_BAK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;;
    --archive) ARCHIVE="$2"; shift;;
    --crontab) CRON_BAK="$2"; shift;;
  esac
  shift
done

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
run_remote() {
  if [ "$APPLY" = "1" ]; then ssh -o BatchMode=yes -o ConnectTimeout=8 "$VPS2" "$@"
  else echo "  DRY: ssh $VPS2 <<$*>>"; fi
}

say "IndiaMART restore — target=$VPS2 apply=$APPLY"
# Resolve defaults on the remote.
if [ -z "$ARCHIVE" ]; then
  ARCHIVE="$(run_remote "ls -1t /home/c03rad0r/indiamart-archive-*.tar.gz 2>/dev/null | head -1")"
fi
if [ -z "$CRON_BAK" ]; then
  CRON_BAK="$(run_remote "ls -1t /home/c03rad0r/indiamart-crontab-*.bak 2>/dev/null | head -1")"
fi
say "archive=$ARCHIVE crontab=$CRON_BAK"

run_remote "tar xzf '$ARCHIVE' -C '$REMOTE_DIR' && echo files-restored"
run_remote "crontab '$CRON_BAK' && echo crontab-restored"
say "restored. Next cron fire resumes the scraper."
