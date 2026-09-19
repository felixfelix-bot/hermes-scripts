#!/bin/bash
# nostr-kanban-inbound-sync.sh — Import kanban state from Nostr
#
# Two functions:
#   1. Nostr kanbanstr → inbound board (kind 30302) from external collaborators
#   2. Nostr → ALL boards bidirectional replication (kind 38010) from peer machine
#
# Counterpart to nostr-kanban-sync.sh (outbound).
# Run as: hermes cron --no-agent --script scripts/nostr-kanban-inbound-sync.sh
# Silent when no new items. Creates local shadow tasks on import.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source ~/nostr-glasses/secrets/.env 2>/dev/null || { echo "No Nostr key configured"; exit 0; }

BOARD_PUBKEY="e18a1d171a59d874edd336472afeb3a614d3dc83397dd097e922a99dcee02133"
BOARD_ID="net4sats-human-gate"
RELAYS="wss://relay.damus.io wss://nos.lol wss://relay.ngit.dev"
STATE_FILE=~/.hermes/state/nostr-kanban-inbound-sync.json
LOCAL_DB="/home/c03rad0r/.hermes/kanban/boards/inbound/kanban.db"

mkdir -p "$(dirname "$STATE_FILE")"

MY_PUBKEY=$(nak key public "$NOSTR_SECRET_KEY" 2>/dev/null || echo "")

# ════════════════════════════════════════════════════════════════════════════
# PART 1: Inbound kanbanstr import (kind 30302) — external collaborators
# ════════════════════════════════════════════════════════════════════════════

# Query kind 30302 events filtered by a-tag = 30301:<pubkey>:<board-id>
EVENTS=$(nak req -k 30302 -t "a=30301:${BOARD_PUBKEY}:${BOARD_ID}" $RELAYS 2>/dev/null || echo "")

if [ -n "$EVENTS" ]; then
    RESULT=$(EVENTS_DATA="$EVENTS" MY_PUBKEY="$MY_PUBKEY" STATE_FILE="$STATE_FILE" LOCAL_DB="$LOCAL_DB" python3 << 'PYEOF'
import json, sys, sqlite3, time, os

events_raw = os.environ["EVENTS_DATA"]
my_pubkey  = os.environ["MY_PUBKEY"]
state_file = os.environ["STATE_FILE"]
db_path    = os.environ["LOCAL_DB"]

events = []
for line in events_raw.strip().split("\n"):
    line = line.strip()
    if not line:
        continue
    try:
        events.append(json.loads(line))
    except json.JSONDecodeError:
        pass

imported = set()
if os.path.exists(state_file):
    try:
        with open(state_file) as f:
            imported = set(json.load(f).get("imported_ids", []))
    except (json.JSONDecodeError, KeyError):
        pass

new_cards = []
seen_ids = set()
for ev in events:
    eid = ev.get("id", "")
    if not eid or eid in seen_ids:
        continue
    seen_ids.add(eid)

    if ev.get("pubkey") == my_pubkey:
        continue
    if eid in imported:
        continue

    tags = ev.get("tags", [])
    title = desc = status_tag = card_d = ""
    for tag in tags:
        if len(tag) >= 2:
            if   tag[0] == "title":       title      = tag[1]
            elif tag[0] == "description": desc       = tag[1]
            elif tag[0] == "s":           status_tag = tag[1]
            elif tag[0] == "d":           card_d     = tag[1]

    if not title:
        title = "Nostr card: " + (card_d or eid[:12])

    status_map = {
        "todo": "todo", "backlog": "todo",
        "doing": "running", "inprogress": "running", "progress": "running",
        "review": "blocked", "humanreview": "blocked", "blocked": "blocked",
        "done": "done", "closed": "done", "complete": "done",
    }
    local_status = status_map.get(status_tag.lower(), "blocked")

    new_cards.append({
        "event_id": eid,
        "pubkey": ev.get("pubkey", ""),
        "title": title,
        "description": desc,
        "status_tag": status_tag,
        "card_d": card_d,
        "local_status": local_status,
        "created_at": ev.get("created_at", int(time.time())),
    })

if not new_cards:
    print(json.dumps({"created": 0, "scanned": len(events)}))
    sys.exit(0)

created = []
conn = sqlite3.connect(db_path)
c = conn.cursor()

for card in new_cards:
    eid = card["event_id"]
    task_id = "nostr-" + eid[:16]

    body = (
        "Imported from Nostr kanbanstr (kind 30302)\n\n"
        "Event ID: " + eid + "\n"
        "Author pubkey: " + card["pubkey"] + "\n"
        "Card UUID (d-tag): " + card["card_d"] + "\n"
        "Nostr status: " + card["status_tag"] + "\n\n"
        + card["description"] + "\n\n"
        "---\n_Synced by nostr-kanban-inbound-sync.sh_"
    )

    try:
        c.execute(
            "INSERT INTO tasks "
            "(id, title, body, assignee, status, priority, "
            " created_by, created_at, workspace_kind) "
            "VALUES (?, ?, ?, 'inbound', ?, 5, "
            " 'nostr-sync', ?, 'scratch')",
            (task_id, card["title"], body, card["local_status"], card["created_at"]),
        )
        created.append(eid)
    except sqlite3.IntegrityError:
        pass

conn.commit()
conn.close()

imported.update(created)
with open(state_file, "w") as f:
    json.dump({"imported_ids": sorted(imported)}, f, indent=2)

print(json.dumps({"created": len(created), "scanned": len(events)}))
PYEOF
    )

    CREATED=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created',0))" 2>/dev/null || echo 0)
    if [ "$CREATED" -gt 0 ]; then
        echo "📥 Imported $CREATED external Nostr card(s) into inbound board"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# PART 2: All-board bidirectional replication (kind 38010) — peer machine sync
# ════════════════════════════════════════════════════════════════════════════
# Subscribes to kind 38010 events from the peer machine and applies them to
# the local kanban.db. Counterpart: nostr-kanban-sync.sh publishes outbound.

REPLICATE_LOG=~/.hermes/state/kanban-nostr-sync.log
if ! python3 "$SCRIPT_DIR/kanban-nostr-replicate.py" --inbound 2>>"$REPLICATE_LOG"; then
    echo "⚠️ kanban nostr inbound replication FAILED — tail $REPLICATE_LOG"
fi
