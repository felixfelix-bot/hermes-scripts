#!/usr/bin/env bash
# indiamart_decommission.sh — retire the IndiaMART price-monitoring on VPS2.
#
# Recoverable by design: it ARCHIVES before it stops. No file is deleted.
#   archive  -> ~/indiamart-archive-<ts>.tar.gz on VPS2 + pulled to ~/reports/
#   stop     -> price-monitor.js / indiamart-prices.js node parents (+ chrome)
#   disable  -> comment the two crontab lines (crontab backed up in the archive)
#
# Reverse with scripts/fleet/indiamart_restore.sh.
#
# Usage:
#   indiamart_decommission.sh            # dry-run (default): prints the plan
#   indiamart_decommission.sh --apply    # do it (idempotent)
#   VPS2_HOST=debian@23.182.128.51 ...
#
# Operator decision 2026-09-17 (archived, recoverable).
set -uo pipefail

VPS2="${VPS2_HOST:-debian@23.182.128.51}"
REMOTE_DIR="${INDIAMART_REMOTE_DIR:-/home/c03rad0r/automation}"
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_REMOTE="/home/c03rad0r/indiamart-archive-${TS}.tar.gz"
CRON_BAK_REMOTE="/home/c03rad0r/indiamart-crontab-${TS}.bak"
LOCAL_DIR="$HOME/reports/indiamart-archive-${TS}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
run_remote() {  # run a command on VPS2 (or echo in dry-run)
  if [ "$APPLY" = "1" ]; then ssh -o BatchMode=yes -o ConnectTimeout=8 "$VPS2" "$@"
  else echo "  DRY: ssh $VPS2 <<$*>>"; fi
}

say "IndiaMART decommission — target=$VPS2 dir=$REMOTE_DIR apply=$APPLY"

# 1) ARCHIVE (tar subset + crontab backup) on VPS2.
run_remote "cd '$REMOTE_DIR' && tar czf '$ARCHIVE_REMOTE' indiamart* indiart* price-monitor.js price-monitor.log price-intel*.json price-data 2>/dev/null; crontab -l > '$CRON_BAK_REMOTE' 2>/dev/null; echo archived=\$(ls -l '$ARCHIVE_REMOTE' 2>/dev/null | awk '{print \$5}')"

# 2) STOP the node parents (chrome children exit with them).
run_remote "pkill -f 'price-monitor.js' 2>/dev/null; pkill -f 'indiamart-prices.js' 2>/dev/null; sleep 2; echo remaining=\$(pgrep -fc 'price-monitor.js|indiamart-prices.js' 2>/dev/null || echo 0)"

# 3) DISABLE the two crontab lines (comment them; keep the text for restore).
run_remote "crontab -l 2>/dev/null | sed -E 's#^([^#].*(price-monitor\.js|indiamart-prices\.js).*)\$#\# DISABLED 2026-09-17 IndiaMART decommission \1#' | crontab - && echo crontab-updated"

# 4) PULL the archive locally.
if [ "$APPLY" = "1" ]; then
  mkdir -p "$LOCAL_DIR"
  scp -q -o BatchMode=yes "$VPS2:$ARCHIVE_REMOTE" "$LOCAL_DIR/" 2>/dev/null \
    && say "local archive: $LOCAL_DIR/$(basename "$ARCHIVE_REMOTE")" \
    || say "WARN: local pull failed (archive still on VPS2: $ARCHIVE_REMOTE)"
else
  echo "  DRY: mkdir -p $LOCAL_DIR && scp $VPS2:$ARCHIVE_REMOTE $LOCAL_DIR/"
fi

say "done. restore with: scripts/fleet/indiamart_restore.sh --apply --archive $ARCHIVE_REMOTE --crontab $CRON_BAK_REMOTE"
