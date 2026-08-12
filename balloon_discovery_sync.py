#!/usr/bin/env python3
"""
Balloon Discovery Sync — scans all 9 balloon track worktrees for cross-relevant
commits, writes them to DISCOVERIES.md, and notifies relevant track Signal groups.

Runs as a Hermes cron job (every 2h). State persisted in balloon-discovery-sync-state.json.

Trigger tags in commit messages: BREAKTHROUGH, TECHNIQUE, RESULT, DISCOVERY, FINDING.
Also flags commits touching shared files (firmware/main/, firmware/components/, docs/coordination/).

Usage:
    python3 balloon_discovery_sync.py           # full run (scan + write + notify)
    python3 balloon_discovery_sync.py --dry-run # scan only, no write/notify
    python3 balloon_discovery_sync.py --json    # output JSON instead of text
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────

HOME = Path.home()
REPO = HOME / "repos" / "balloon-fresh"
COORD_DIR = REPO / "docs" / "coordination"
REGISTRY_PATH = COORD_DIR / "TRACKS-REGISTRY.yaml"
DISCOVERIES_PATH = COORD_DIR / "DISCOVERIES.md"
STATE_PATH = HOME / ".hermes" / "profiles" / "manager" / "state" / "balloon-discovery-sync-state.json"

# Tags that mark a commit as cross-relevant
TRIGGER_TAGS = ["BREAKTHROUGH", "TECHNIQUE", "RESULT", "DISCOVERY", "FINDING"]

# File path patterns that indicate cross-relevant changes (in commit diff)
SHARED_FILE_PATTERNS = [
    r"firmware/main/",
    r"firmware/components/",
    r"docs/coordination/",
    r"AGENTS\.md",
    r"tracker/hardware/",
]

# Relevance classification keywords → tag mapping
RELEVANCE_MAP = {
    "SPI":      [r"\bSPI\b", r"single.batch", r"spi_batch", r"MOSI", r"MISO", r"SCLK", r"spi_"],
    "RADIO":    [r"\bLoRa\b", r"\bFLRC\b", r"\bGFSK\b", r"LR2021", r"SX128", r"RadioLib", r"modulat", r"throughput", r"kbps", r"packet.loss"],
    "POWER":    [r"\bsolar\b", r"supercap", r"\bLDO\b", r"power.manag", r"\bBMP280\b", r"battery", r"voltage"],
    "FIRMWARE": [r"\bESP-IDF\b", r"idf\.py", r"\bbuild\b", r"cmake", r"component", r"\bC3\b.*firmware", r"flash"],
    "HARDWARE": [r"\bPCB\b", r"\bGPIO\b", r"pin.assign", r"JLCPCB", r"SKiDL", r"KiCad", r"schematic", r"footprint", r"Gerber"],
    "PROTOCOL": [r"\bmesh\b", r"\brouting\b", r"\bTDMA\b", r"erasure.cod", r"relay", r"\bnostr\b", r"blossom"],
    "TEST":     [r"\btest\b", r"\bresult\b", r"range.sweep", r"outdoor", r"baseline", r"measurement"],
}

# Track → which relevance tags it cares about
TRACK_RELEVANCE = {
    "balloon-hermes":          ["SPI", "RADIO", "POWER", "FIRMWARE", "HARDWARE", "PROTOCOL", "TEST"],
    "balloon-fips":             ["SPI", "RADIO", "FIRMWARE", "PROTOCOL"],
    "balloon-range-tests":      ["SPI", "RADIO", "TEST"],
    "balloon-speed-tests":      ["SPI", "RADIO", "TEST"],
    "balloon-circuit-design":   ["HARDWARE", "SPI", "POWER"],
    "balloon-tollgate":         ["FIRMWARE", "PROTOCOL"],
    "balloon-pow":              ["FIRMWARE", "POWER"],
    "balloon-nostr":            ["PROTOCOL", "FIRMWARE"],
    "balloon-blossom":          ["PROTOCOL"],
    "balloon-pre-stretching":   ["HARDWARE", "POWER"],
}

# Signal group IDs from TRACKS-REGISTRY.yaml (populated at runtime from YAML)
# Fallback mapping if YAML parse fails:
SIGNAL_GROUP_FALLBACK = {
    "balloon-hermes":          "group:DsaZYWqljekDA4/cBK9NLDVrudr7rkqcVhI02Z0/M08=",
    "balloon-nostr":            "group:yZ5t/RwJR+crH+551nA2jWAeCF7l7ZISf0xnbPf7JRM=",
    "balloon-tollgate":         "group:98/vxkbtZ/xalrb2/iQgq9NF18lR3+Pi4OBc3cwgFtw=",
    "balloon-pow":              "group:nJ2nVXfigVWaC+wEBZlg4SwHo6YLwTtVy+G4EP/EK9o=",
    "balloon-fips":             "group:OQqq+8cMjrbZ7XzAyH8gdZLHltX8z6ChcUBntbAyE1M=",
    "balloon-blossom":          "group:Yi2On6xci0JEFn3hIkhJterfs4k3foG5jrdqJKmIZtc=",
    "balloon-range-tests":      "group:gZ79Pz43XBbb0t0Fh+c7HHLp31iZlMg/Qng7ACz6Rec=",
    "balloon-speed-tests":      "group:LjZcVC7k0P9dA3YNs/yNdifyaUlqDO0H4vy+/wZ37mc=",
    "balloon-pre-stretching":   "group:u45QgoCt/GB3KwXza4eM+2GmijwZyJys4053Pe4Ipk0=",
    "balloon-circuit-design":   "group:8qBP7aZufyKFburu6I6LVhG1F8MxudiBbwDek8cBqMk=",
}

# ── State Management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load last-sync state (per-track last-seen commit hash)."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_sync": None, "track_commits": {}}

def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_sync"] = datetime.now().isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))

# ── Registry Loading ──────────────────────────────────────────────────

def load_registry() -> dict | None:
    """Load TRACKS-REGISTRY.yaml. Returns dict or None if parse fails."""
    try:
        import yaml
        with open(REGISTRY_PATH) as f:
            return yaml.safe_load(f)
    except Exception:
        return None

def get_signal_groups(registry: dict | None) -> dict[str, str]:
    """Extract track_name → signal_group_id mapping from registry."""
    groups = {}
    if registry:
        for track in registry.get("tracks", []):
            gid = track.get("signal_group_id")
            if gid:
                groups[track["name"]] = f"group:{gid}"
    # Merge with fallback for any missing
    groups.update(SIGNAL_GROUP_FALLBACK)
    return groups

# ── Git Scanning ──────────────────────────────────────────────────────

def get_new_commits(worktree: str, last_hash: str | None) -> list[dict]:
    """Get commits since last_hash from a worktree.
    
    Returns list of {hash, message, author, date, files} dicts.
    """
    worktree = os.path.expanduser(worktree)
    if not Path(worktree).is_dir():
        return []

    # Build git log command
    cmd = ["git", "log", "--format=%H|%s|%an|%ai", "--name-only"]
    if last_hash:
        cmd.append(f"{last_hash}..HEAD")
    else:
        cmd.append("-20")  # First run: last 20 commits

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=worktree, timeout=15)
        if result.returncode != 0:
            return []
    except Exception:
        return []

    commits = []
    current = None
    for line in result.stdout.strip().splitlines():
        if "|" in line and line.count("|") >= 3:
            parts = line.split("|", 3)
            current = {
                "hash": parts[0],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
                "files": [],
            }
            commits.append(current)
        elif current and line.strip():
            current["files"].append(line.strip())

    return commits

def classify_relevance(commit: dict) -> list[str]:
    """Classify a commit's relevance tags based on message + file paths."""
    text = commit["message"] + " " + " ".join(commit["files"])
    tags = []

    # Check trigger tags first — if present, this is definitely cross-relevant
    has_trigger = any(tag in commit["message"].upper() for tag in TRIGGER_TAGS)

    # Check shared file patterns
    touches_shared = any(
        re.search(pat, " ".join(commit["files"]), re.IGNORECASE)
        for pat in SHARED_FILE_PATTERNS
    )

    if not has_trigger and not touches_shared:
        return []  # Not cross-relevant

    # Classify into relevance categories
    for tag, patterns in RELEVANCE_MAP.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            tags.append(tag)

    # If trigger tag present but no specific relevance matched, tag as GENERAL
    if not tags:
        tags.append("GENERAL")

    return tags

# ── Discoveries File Writing ──────────────────────────────────────────

def format_discovery(track: str, commit: dict, tags: list[str]) -> str:
    """Format a discovery entry for DISCOVERIES.md."""
    date = commit["date"][:10] if commit["date"] else "unknown"
    short_hash = commit["hash"][:7]
    tag_str = ", ".join(tags)
    files_summary = ", ".join(commit["files"][:3])
    if len(commit["files"]) > 3:
        files_summary += f" (+{len(commit['files']) - 3} more)"

    return (
        f"### [{track}] {commit['message'][:80]} ({date}) | tags: {tag_str}\n"
        f"- **Commit:** `{short_hash}` by {commit['author']}\n"
        f"- **Files:** {files_summary}\n"
        f"- **Full message:** {commit['message']}\n"
        f"- **Relevance:** {', '.join(tags)}\n"
    )

def update_discoveries_file(new_entries: list[tuple[str, dict, list[str]]]) -> int:
    """Append new discovery entries to DISCOVERIES.md. Returns count written."""
    if not new_entries:
        return 0

    current = DISCOVERIES_PATH.read_text() if DISCOVERIES_PATH.exists() else ""

    # Find the insertion point (before the footer comment, or at end)
    marker = "<!-- New discoveries are appended below. Do not edit existing entries. -->"
    lines = []
    for track, commit, tags in new_entries:
        lines.append(format_discovery(track, commit, tags))

    block = "\n" + "\n".join(lines)

    if marker in current:
        # Insert after marker line
        parts = current.split(marker, 1)
        updated = parts[0] + marker + "\n" + block + parts[1]
    else:
        updated = current.rstrip() + "\n" + block + "\n"

    DISCOVERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERIES_PATH.write_text(updated)
    return len(new_entries)

# ── Signal Notification ────────────────────────────────────────────────

def notify_track(track_name: str, signal_target: str, discoveries: list[dict], dry_run: bool = False):
    """Send a brief notification to a track's Signal group about relevant discoveries."""
    if not discoveries:
        return

    # Build concise notification (Signal = no markdown)
    lines = [f"[DISCOVERY SYNC] {len(discoveries)} new cross-relevant finding(s) for {track_name}:"]
    for d in discoveries[:5]:  # Max 5 per notification
        src = d["source_track"]
        short = d["message"][:70]
        tags = ", ".join(d["tags"])
        lines.append(f"  - {src}: {short} [{tags}]")
    if len(discoveries) > 5:
        lines.append(f"  ... and {len(discoveries) - 5} more. See DISCOVERIES.md.")
    lines.append("Check ~/repos/balloon-fresh/docs/coordination/DISCOVERIES.md for details.")
    lines.append("Do NOT coordinate with other tracks. Use findings independently.")

    message = "\n".join(lines)

    if dry_run:
        print(f"[DRY RUN] Would send to {signal_target}:\n{message}\n")
        return

    try:
        subprocess.run(
            ["hermes", "send", "--to", f"signal:{signal_target}", message],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Failed to notify {track_name}: {e}", file=sys.stderr)

# ── Main ──────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    output_json = "--json" in sys.argv

    state = load_state()
    registry = load_registry()
    signal_groups = get_signal_groups(registry)

    tracks = []
    if registry:
        tracks = registry.get("tracks", [])
    # Fallback: build from worktree dirs
    if not tracks:
        for d in sorted((HOME / "worktrees").glob("balloon-*")):
            tracks.append({"name": d.name, "worktree": str(d), "signal_group_id": None})

    all_discoveries = []       # For DISCOVERIES.md
    notifications = {}          # track_name → list of discovery summaries

    for track in tracks:
        name = track["name"]
        worktree = track["worktree"]
        last_hash = state.get("track_commits", {}).get(name)

        commits = get_new_commits(worktree, last_hash)
        if not commits:
            continue

        # Update last-seen hash (HEAD commit = first in log output)
        state.setdefault("track_commits", {})[name] = commits[0]["hash"]

        # Filter for cross-relevant commits
        for commit in commits:
            tags = classify_relevance(commit)
            if not tags:
                continue

            all_discoveries.append((name, commit, tags))

            # Determine which OTHER tracks should be notified
            for other_track, other_tags in TRACK_RELEVANCE.items():
                if other_track == name:
                    continue  # Don't notify the source track
                if any(t in other_tags for t in tags):
                    notifications.setdefault(other_track, []).append({
                        "source_track": name,
                        "message": commit["message"],
                        "tags": tags,
                        "hash": commit["hash"][:7],
                    })

    # Write discoveries to file
    written = update_discoveries_file(all_discoveries) if not dry_run else 0

    # Send notifications
    notified = 0
    if not dry_run:
        for track_name, discoveries in notifications.items():
            sig_target = signal_groups.get(track_name)
            if sig_target:
                notify_track(track_name, sig_target, discoveries)
                notified += 1

    # Save state
    if not dry_run:
        save_state(state)

    # Output
    summary = {
        "timestamp": datetime.now().isoformat(),
        "tracks_scanned": len(tracks),
        "new_commits_found": sum(1 for _, c, _ in all_discoveries),
        "discoveries_written": written,
        "tracks_notified": notified,
        "notifications": {
            t: len(d) for t, d in notifications.items()
        },
        "dry_run": dry_run,
    }

    if output_json:
        print(json.dumps(summary, indent=2))
    elif summary["new_commits_found"] > 0 or summary["discoveries_written"] > 0:
        # Only print when there IS something to report — silent on idle
        print(f"Balloon Discovery Sync — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"  Tracks scanned: {summary['tracks_scanned']}")
        print(f"  Cross-relevant commits found: {summary['new_commits_found']}")
        print(f"  Discoveries written to DISCOVERIES.md: {summary['discoveries_written']}")
        print(f"  Tracks notified: {summary['tracks_notified']}")
        if notifications:
            print(f"  Notifications breakdown:")
            for t, count in summary["notifications"].items():
                print(f"    {t}: {count} finding(s)")
        if dry_run:
            print("  [DRY RUN — no files written, no notifications sent]")
    # else: SILENT — no output when nothing happened (zero tokens, no spam)

    return 0

if __name__ == "__main__":
    sys.exit(main())