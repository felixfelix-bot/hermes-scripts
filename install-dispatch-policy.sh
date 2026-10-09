#!/usr/bin/env bash
# install-dispatch-policy.sh - atomic, config-as-code install of the dispatch gate.
#
# Ships three files TOGETHER: staggered-dispatch.sh, dispatch_gate.py and
# config/dispatch_policy.json. Installing the script alone leaves check_resources
# without a gate -> fail-closed -> all dispatch stops, loudly.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/.hermes/scripts}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DEST/.backup-dispatch-$STAMP"

command -v python3 >/dev/null || { echo "FAIL: python3 not found" >&2; exit 1; }
[ -f "$SRC/dispatch_gate.py" ] || { echo "FAIL: $SRC/dispatch_gate.py missing" >&2; exit 1; }
[ -f "$SRC/config/dispatch_policy.json" ] || { echo "FAIL: config/dispatch_policy.json missing" >&2; exit 1; }
[ -f "$SRC/staggered-dispatch.sh" ] || { echo "FAIL: staggered-dispatch.sh missing" >&2; exit 1; }

mkdir -p "$DEST/config" "$BACKUP"
for f in staggered-dispatch.sh dispatch_gate.py; do
    [ -e "$DEST/$f" ] && cp -p "$DEST/$f" "$BACKUP/$f" || true
done
[ -e "$DEST/config/dispatch_policy.json" ] && cp -p "$DEST/config/dispatch_policy.json" "$BACKUP/dispatch_policy.json" || true

install -m 0755 "$SRC/dispatch_gate.py"      "$DEST/dispatch_gate.py"
install -m 0644 "$SRC/config/dispatch_policy.json" "$DEST/config/dispatch_policy.json"
install -m 0755 "$SRC/staggered-dispatch.sh" "$DEST/staggered-dispatch.sh"

# Pre-flight: the gate must run and the board list must resolve, or roll back.
if ! "$DEST/dispatch_gate.py" >/dev/null 2>&1; then
    rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "FAIL: dispatch_gate.py errored (rc=$rc) - rolling back" >&2
        cp -p "$BACKUP/staggered-dispatch.sh" "$DEST/staggered-dispatch.sh" 2>/dev/null || true
        cp -p "$BACKUP/dispatch_gate.py" "$DEST/dispatch_gate.py" 2>/dev/null || true
        cp -p "$BACKUP/dispatch_policy.json" "$DEST/config/dispatch_policy.json" 2>/dev/null || true
        exit 1
    fi
fi
boards="$("$DEST/dispatch_gate.py" --boards | tr '\n' ' ')"
[ -n "$boards" ] || { echo "FAIL: empty board list" >&2; exit 1; }

echo "installed dispatch policy -> $DEST"
echo "boards: $boards"
echo "backup: $BACKUP"
