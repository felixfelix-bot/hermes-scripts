#!/usr/bin/env bash
# install-fleet-timers.sh - install the fleet's systemd *user* timers on a node.
#
# WHY: the heartbeat is what publishes a node's capacity AND its fit profile
# (repos/capabilities) to the fleet. A node without it is invisible: the fleet
# cannot match any task whose requirements name a repo that node actually has,
# so work advertised to that node is never claimed.
#
# The units live in systemd/user/ as config-as-code. This installer is the only
# supported way to put them on a node. Idempotent; --dry-run shows what changes.
#
# Usage:
#   install-fleet-timers.sh [--dry-run] [--linger] [--units-dir DIR]
set -euo pipefail

DRY=0
LINGER=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd/user"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --linger)  LINGER=1 ;;
    --units-dir) SRC="${2:?}"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"
SCRIPTS="$HOME/.hermes/scripts"
TIMERS="fleet-heartbeat.timer fleet-resource-collector.timer fleet-arbiter.timer"
SERVICES="fleet-heartbeat fleet-resource-collector fleet-arbiter"

[ -d "$SRC" ] || { echo "ERROR: units dir not found: $SRC" >&2; exit 1; }

# Pre-flight: a timer whose script is missing fails every 30s and publishes nothing.
missing=0
for s in fleet_heartbeat.py fleet_resource_collector.py fleet_arbiter.py; do
  if [ ! -f "$SCRIPTS/$s" ]; then echo "WARN: missing $SCRIPTS/$s (that timer will fail)"; missing=1; fi
done

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "DRY: $*"; else "$@"; fi; }

run mkdir -p "$DEST"
for name in $SERVICES; do
  src="$SRC/$name.service"; dst="$DEST/$name.service"
  [ -f "$src" ] || { echo "ERROR: $src missing" >&2; exit 1; }
  if [ -f "$dst" ] && cmp -s <(sed "s|@PYTHON@|$PYTHON|g" "$src") "$dst"; then
    say "unchanged: $name.service"
  else
    say "install: $name.service"
    if [ "$DRY" = 0 ]; then sed "s|@PYTHON@|$PYTHON|g" "$src" > "$dst.tmp" && mv "$dst.tmp" "$dst"; fi
  fi
done
for name in $TIMERS; do
  src="$SRC/$name"; dst="$DEST/$name"
  [ -f "$src" ] || { echo "ERROR: $src missing" >&2; exit 1; }
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then say "unchanged: $name"; else say "install: $name"; run cp -f "$src" "$dst"; fi
done

if [ "$DRY" = 0 ]; then
  systemctl --user daemon-reload
  for t in $TIMERS; do systemctl --user enable --now "$t"; done
else
  say "DRY: systemctl --user daemon-reload && enable --now $TIMERS"
fi

if [ "$LINGER" = 1 ]; then
  run loginctl enable-linger "$USER" || say "WARN: enable-linger failed; timers stop at logout"
fi
if command -v loginctl >/dev/null; then
  linger="$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p' || true)"
  say "linger=${linger:-unknown} (needs to be 'yes' for timers to run with no session)"
fi
[ "$missing" = 1 ] && say "WARN: pre-flight found missing scripts; fix before relying on these timers"
say "done. verify: systemctl --user list-timers $TIMERS; ls -l ~/.hermes/bot/fleet_health.json"
