#!/usr/bin/env bash
# ============================================================================
# fleet_dispatch_guard.sh — shared preflight for any per-board dispatch caller.
#
# OPERATOR 2026-09-11 (H.3): dozens of watchdogs/crons each called
# `hermes kanban --board X dispatch` independently, fanning out beyond the
# fleet cap. This is the single admission check they all consult BEFORE
# spawning a dispatcher process. The authoritative spawn-time gate remains
# kanban-crash-wrapper.sh; this simply stops the redundant work earlier and
# keeps all per-board loops on one policy.
#
# Exit 0 = OK to dispatch. Exit 1 = hold (frozen / quarantined / at cap).
# Prints a one-line reason to stdout when holding. Safe to call every tick.
# ============================================================================
set -uo pipefail

BOT="${HERMES_BOT_DIR:-$HOME/.hermes/bot}"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
_CAP="${HERMES_FLEET_CAP:-4}"

# Optional throttle cap written by fleet_remediate.py (action=throttle).
if [[ -f "$BOT/.fleet_cap" ]]; then
    _fc="$(grep -oE '"cap"[[:space:]]*:[[:space:]]*[0-9]+' "$BOT/.fleet_cap" 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    if [[ -n "$_fc" && "$_fc" -lt "$_CAP" ]]; then
        _CAP="$_fc"
    fi
fi

if [[ -e "$HERMES_HOME_DIR/ESTOP" ]]; then
    echo "fleet-guard: estop"
    exit 1
fi
if [[ -e "$BOT/.dispatch_frozen" ]]; then
    echo "fleet-guard: dispatch_frozen"
    exit 1
fi
if [[ -e "$BOT/.fleet_quarantine" ]]; then
    echo "fleet-guard: quarantine"
    exit 1
fi

_slots=0
if [[ -d "$BOT/.fleet_slots" ]]; then
    for _sf in "$BOT/.fleet_slots"/*.slot; do
        [[ -e "$_sf" ]] || continue
        _sp="$(basename "$_sf" .slot)"
        if kill -0 "$_sp" 2>/dev/null; then
            _slots=$((_slots + 1))
        else
            rm -f "$_sf" 2>/dev/null || true
        fi
    done
fi
if [[ "$_slots" -ge "$_CAP" ]]; then
    echo "fleet-guard: fleet_cap($_slots>=$_CAP)"
    exit 1
fi

# Headroom governor (fresh only): an explicit can_dispatch=false holds dispatch.
_H="$BOT/dispatch_headroom.json"
if [[ -f "$_H" ]]; then
    _mtime=$(stat -c %Y "$_H" 2>/dev/null || echo 0)
    _age=$(( $(date +%s) - _mtime ))
    if [[ "$_age" -lt 900 ]]; then
        if grep -qE '"can_dispatch"[[:space:]]*:[[:space:]]*false' "$_H" 2>/dev/null; then
            echo "fleet-guard: headroom can_dispatch=false"
            exit 1
        fi
    fi
fi

exit 0
