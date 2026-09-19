#!/bin/bash
# deploy-bidirectional-sync.sh — wire the upgraded bidirectional Nostr kanban
# sync into the MANAGER profile (where the cron jobs already run).
#
# WHY A SCRIPT: the upgraded scripts live in the shared ~/.hermes/scripts/net4sats/
# but the manager cron resolves bare names from ~/.hermes/profiles/manager/scripts/.
# Copying the upgraded scripts there WITH THE SAME NAMES lets the existing every-15m
# cron jobs pick them up with ZERO cron-config change.
#
# This touches the manager profile, so it is intentionally NOT run automatically by
# the worker. Review it, then run:  bash ~/.hermes/scripts/net4sats/deploy-bidirectional-sync.sh
#
# What it does:
#   1. Backs up every target manager script (timestamped suffix)
#   2. Copies 3 upgraded files into manager/scripts/ (2 shell + kanban-nostr-replicate.py)
#      — SKIPPING any file whose live copy has drifted, unless FORCE=1
#   3. Verifies the shell scripts now show the "Two functions" bidirectional header
#   4. Warns if no cron job actually runs the OUTBOUND script
#
# Reversible: restore from the .bak.<timestamp> files.
set -euo pipefail

SRC="$HOME/.hermes/scripts/net4sats"
DST="$HOME/.hermes/profiles/manager/scripts"
CRON_JOBS="$HOME/.hermes/profiles/manager/cron/jobs.json"
FILES="nostr-kanban-sync.sh nostr-kanban-inbound-sync.sh kanban-nostr-replicate.py"
TS="$(date +%Y%m%d-%H%M%S)"

echo "=== bidirectional kanban-nostr sync deploy ($TS) ==="
echo "src: $SRC"
echo "dst: $DST"
echo

for f in $FILES; do
    [ -f "$SRC/$f" ] || { echo "FAIL: missing $SRC/$f"; exit 1; }
done
mkdir -p "$DST"

# 1. Back up every file we are about to touch
for f in $FILES; do
    if [ -f "$DST/$f" ]; then
        cp -p "$DST/$f" "$DST/$f.bak.$TS"
        echo "  backed up $f -> $f.bak.$TS"
    fi
done

# 2. Copy upgraded files — but NEVER silently regress the live profile.
# The live manager copies have historically carried edits made AFTER a deploy
# (relay set widened to relay.ngit.dev; import board renamed human-gate -> inbound).
# Blindly overwriting them from the shared source would roll those back without a
# word, so a file that differs from its live copy is skipped unless FORCE=1.
DRIFT=""
for f in $FILES; do
    if [ -f "$DST/$f" ] && ! cmp -s "$SRC/$f" "$DST/$f"; then
        if [ "${FORCE:-0}" = "1" ]; then
            echo "  FORCE: overwriting live $f (differs from staged)"
        else
            echo "  SKIP $f — live copy differs from staged (local drift):"
            diff "$SRC/$f" "$DST/$f" || true
            DRIFT="$DRIFT $f"
            continue
        fi
    fi
    cp "$SRC/$f" "$DST/$f"
done
chmod +x "$DST/nostr-kanban-sync.sh" "$DST/nostr-kanban-inbound-sync.sh"
echo "  staged files copied to $DST"
if [ -n "$DRIFT" ]; then
    echo "  NOTE: kept the live version of:$DRIFT"
    echo "        Reconcile staged with live (cp $DST/<f> $SRC/<f>) before re-deploying."
fi

# 3. Verify
echo
echo "=== verify ==="
head -7 "$DST/nostr-kanban-sync.sh" | grep -q "Two functions" \
    && echo "  OK outbound script is bidirectional" || { echo "  FAIL: outbound not upgraded"; exit 1; }
head -7 "$DST/nostr-kanban-inbound-sync.sh" | grep -q "Two functions" \
    && echo "  OK inbound script is bidirectional" || { echo "  FAIL: inbound not upgraded"; exit 1; }
python3 -c "import ast; ast.parse(open('$DST/kanban-nostr-replicate.py').read())" \
    && echo "  OK replicate.py parses" || { echo "  FAIL: replicate.py syntax error"; exit 1; }

# 4. Both halves must actually be SCHEDULED, or "bidirectional" is fiction.
# The 2026-07 outbound job (610dc49e4841, every 15m no_agent) was paused and later
# removed from the manager cron store entirely; the sync silently became
# inbound-only and the outbound watermark froze at 2026-07-03. Never assume.
echo
echo "=== cron wiring ==="
if grep -q '"nostr-kanban-sync\.sh"' "$CRON_JOBS" 2>/dev/null; then
    echo "  OK a manager cron job references nostr-kanban-sync.sh (outbound)"
else
    echo "  WARNING: NO manager cron job runs nostr-kanban-sync.sh."
    echo "           The OUTBOUND half is not scheduled — nothing gets published,"
    echo "           so the peer machine receives no deltas. Re-create the job"
    echo "           (no_agent, script=nostr-kanban-sync.sh, every 15m) in the"
    echo "           manager profile, then confirm ~/.hermes/state/kanban-nostr-outbound.json"
    echo "           starts updating."
fi
if grep -q '"nostr-kanban-inbound-sync\.sh"' "$CRON_JOBS" 2>/dev/null; then
    echo "  OK a manager cron job references nostr-kanban-inbound-sync.sh (inbound)"
else
    echo "  WARNING: no manager cron job runs nostr-kanban-inbound-sync.sh (inbound)."
fi

echo
echo "=== deploy complete ==="
echo
echo "NOTE on first live outbound run: the outbound watermark is normally already at"
echo "'current', so only NEW deltas accrued after this deploy are published — correct"
echo "delta-sync behavior. DQ05 gets its full baseline via SSHFS (M2b), then receives"
echo "future deltas over Nostr. To force a full re-publish, delete"
echo "~/.hermes/state/kanban-nostr-outbound.json and re-run '--seed' on each machine."
echo
echo "Relays: wss://relay.damus.io wss://nos.lol wss://relay.ngit.dev  (kind 38010,"
echo "parameterized replaceable per NIP-33; d-tag = <hostname>:<board>:<entity>)."
echo "Board 'art-jeff' is in EXCLUDE_BOARDS and is never published or applied."
echo
echo "To roll back: restore the .bak.$TS files in $DST"
