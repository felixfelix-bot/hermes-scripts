#!/usr/bin/env python3
"""One-off retention prune for state.db.snap-* files.

Keeps the newest KEEP_RECENT snapshots plus the newest snapshot per UTC day for
the last DAILY_KEEP days, in each given directory. Prints what it deletes.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP_RECENT = int(sys.argv[1])
DAILY_KEEP = int(sys.argv[2])
DIRS = [Path(p) for p in sys.argv[3:]]

for d in DIRS:
    snaps = []
    for p in sorted(d.glob("state.db.snap-*")):
        if p.name.endswith(("-wal", "-shm")):
            continue
        snaps.append((p, p.stat().st_mtime))
    snaps.sort(key=lambda x: x[1], reverse=True)  # newest first
    keep = {p for p, _ in snaps[:KEEP_RECENT]}
    per_day = {}
    for p, mt in snaps:
        per_day.setdefault(datetime.fromtimestamp(mt, timezone.utc).date(), p)
    for day in sorted(per_day)[-DAILY_KEEP:]:
        keep.add(per_day[day])
    freed = 0
    removed = 0
    for p, _ in snaps:
        if p in keep:
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            Path(str(p) + "-wal").unlink(missing_ok=True)
            Path(str(p) + "-shm").unlink(missing_ok=True)
            freed += size
            removed += 1
        except OSError as exc:
            print("  FAILED", p.name, exc)
    print(f"{d}: kept={len(keep)} removed={removed} freed={freed/1e9:.2f}GB")
