#!/usr/bin/env bash
# routstrd_lane_guard.sh — single-instance guard for the routstrd buy lane.
#
# WHY: the lane used to be a stray, hand-started daemon (reparented to
# systemd --user, no unit). systemd owns exactly one instance of routstrd.service
# — if anything else already holds the lane port, starting ours either fails to
# bind (and, with Restart=always, spins: the 2026-09-20 park recorded counter
# 27753) or leaves two processes racing for the same Cashu wallet. This script is
# wired as ExecStartPre so a second instance can never start, and it prints the
# exact remediation instead of failing with a bare "address in use".
#
# Modes
#   --check (default)  diagnose; exit 3 when a FOREIGN process holds the port
#   --reclaim          terminate the foreign holder, but only if its cmdline
#                      matches the routstrd daemon entry (opt-in; --dry-run
#                      prints what it would do)
#   --status           human summary of who holds the lane; always exit 0
#
# Options
#   --port <n>         lane port (default 8008)
#   --service <unit>   owning unit name (default routstrd.service)
#   --dry-run          with --reclaim: report, do not kill
#
# Exit codes: 0 free / already ours / reclaimed; 2 usage error; 3 foreign holder.
set -u

PORT=8008
SERVICE=routstrd.service
MODE=check
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check)     MODE=check; shift ;;
    --reclaim)   MODE=reclaim; shift ;;
    --status)    MODE=status; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --port)      PORT="${2:-}"; shift 2 ;;
    --service)   SERVICE="${2:-}"; shift 2 ;;
    -h|--help)   sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "guard: unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) echo "guard: invalid --port: '$PORT'" >&2; exit 2 ;;
esac

self_cgroup_matches() { # pid
  grep -q -- "$SERVICE" "/proc/$1/cgroup" 2>/dev/null
}
cmdline_of() { # pid
  tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true
}
# A process we are allowed to reclaim: it must BE the routstrd daemon, matched
# on the documented entrypoint — not merely contain the word "routstrd" (a
# monitoring cron, a test runner or a shell wrapper can contain it too, and
# killing one of those would be a destructive mistake).
looks_like_lane() { # pid
  case "$(cmdline_of "$1")" in
    *routstrd/dist/daemon/index.js*) return 0 ;;
    *routstrd/dist/index.js*daemon*) return 0 ;;
    *) return 1 ;;
  esac
}
listeners() {
  ss -ltnpH "sport = :$PORT" 2>/dev/null \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

describe() { # pid
  echo "  pid=$(printf '%s' "$1")  cgroup=$(awk -F: '{print $3}' "/proc/$1/cgroup" 2>/dev/null || echo '?')"
  echo "    cmd: $(cmdline_of "$1")"
  echo "    exe: $(readlink -f "/proc/$1/exe" 2>/dev/null || echo '?')"
}

pids="$(listeners || true)"
ours=""
foreign=""
for pid in $pids; do
  if self_cgroup_matches "$pid"; then
    ours="$ours $pid"
  else
    foreign="$foreign $pid"
  fi
done

if [ "$MODE" = status ]; then
  echo "routstrd lane guard: port $PORT (unit $SERVICE)"
  if [ -z "$pids" ]; then
    echo "  holder: none (port free)"
  else
    for pid in $pids; do describe "$pid"; done
  fi
  for w in "$HOME/.routstrd/wallet" "$HOME/.cocod"; do
    [ -e "$w/coco.db" ] && printf '  wallet %s/coco.db: %s bytes\n' "$w" "$(stat -c %s "$w/coco.db" 2>/dev/null || echo '?')"
  done
  echo "  tcp: $(ss -ltnpH "sport = :$PORT" 2>/dev/null | tr -s ' ' | head -n1)"
  exit 0
fi

if [ -z "$pids" ]; then
  echo "guard: port $PORT free — ok to start $SERVICE"
  exit 0
fi

if [ -z "$foreign" ]; then
  echo "guard: port $PORT already held by $SERVICE (pid(s):$ours) — managed instance, not a stray"
  exit 0
fi

echo "guard: REFUSING to start a second routstrd instance — port $PORT is held by a process" >&2
echo "guard: that does not belong to $SERVICE:" >&2
for pid in $foreign; do describe "$pid" >&2; done

if [ "$MODE" = reclaim ]; then
  unsafe=0
  for pid in $foreign; do
    if looks_like_lane "$pid"; then
      if [ "$DRY_RUN" = 1 ]; then
        echo "guard: dry-run — would terminate stray pid $pid" >&2
      else
        echo "guard: terminating stray pid $pid (SIGTERM)" >&2
        kill -TERM "$pid" 2>/dev/null || true
      fi
    else
      echo "guard: pid $pid does not look like the routstrd daemon — NOT killing it" >&2
      unsafe=1
    fi
  done
  [ "$DRY_RUN" = 1 ] && exit 3
  for _ in $(seq 1 20); do
    sleep 0.5
    [ -z "$(listeners || true)" ] && break
  done
  remaining="$(listeners || true)"
  if [ -z "$remaining" ]; then
    echo "guard: stray reclaimed — port $PORT now free"
    exit 0
  fi
  echo "guard: still held after reclaim (pid(s): $remaining)" >&2
  [ "$unsafe" = 1 ] && echo "guard: at least one holder was NOT ours to kill — resolve it by hand" >&2
  exit 3
fi

echo "guard: remediation (operator/manager decision, one of):" >&2
echo "guard:   $0 --reclaim --port $PORT          # kill the stray (cmdline-checked)" >&2
echo "guard:   systemctl --user stop routstrd.service; kill $foreign" >&2
exit 3
