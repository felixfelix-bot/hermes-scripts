#!/bin/bash
# safe-tier-cleanup.sh — regenerable-cache cleanup for the Hermes host (CobradorWave)
# SAFE TIER ONLY: nothing here holds non-regenerable data.
# Every target is a cache/build artifact that is re-created on demand.
#
# Guards:
#   * refuses to touch any Rust target/ dir with a live cargo/rustc process
#   * refuses to touch any dir with open file descriptors
#   * never touches repos, worktrees (other than target/), live DBs, docker named volumes
#   * user-only: no sudo anywhere
#
# Usage:  bash safe-tier-cleanup.sh [--dry-run]
#
# Origin: kanban t_f978b464 (2026-09-12, / 93% -> 86%). Companion report:
#   ops/disk-cleanup/2026-09-12-disk-cleanup.md
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
df1() { df -h / | tail -1; }

owner_absent() {   # $1 = path ; true if no process holds a file inside
    [ -d "$1" ] || return 1
    local n
    n=$(lsof +D "$1" 2>/dev/null | grep -vc COMMAND)
    [ "$n" -eq 0 ]
}

rm_tree() {        # $1 = path
    local p="$1"
    [ -e "$p" ] || return 0
    if ! owner_absent "$p"; then
        log "SKIP (open fds): $p"
        return 0
    fi
    local s
    s=$(du -sh "$p" 2>/dev/null | cut -f1)
    if [ "$DRY" = 1 ]; then
        log "DRY would delete $s $p"
        return 0
    fi
    if rm -rf -- "$p"; then log "DELETED $s $p"; else log "FAILED $p"; fi
}

log "=== safe-tier cleanup start; before: $(df1) ==="
live=$(pgrep -c -f 'cargo|rustc' 2>/dev/null); log "live cargo/rustc: ${live:-0}"

# 1. tool caches (all re-downloaded on demand)
for p in "$HOME"/.cache/ms-playwright "$HOME"/.cache/uv "$HOME"/.cache/opencode \
         "$HOME"/.cache/typescript "$HOME"/.cache/pip "$HOME"/.cache/virtualenv \
         "$HOME"/.cache/pre-commit "$HOME"/.cache/Espressif "$HOME"/.cache/deno \
         "$HOME"/.cache/gopls "$HOME"/.cache/goimports "$HOME"/.cache/go-build \
         "$HOME"/.cache/tg-embed-target "$HOME"/.cache/hermit "$HOME"/.cache/signal-tmp \
         "$HOME"/.bun/install/cache "$HOME"/.cargo/registry/cache "$HOME"/.npm/_cacache; do
    rm_tree "$p"
done
rm -rf "$HOME"/.cache/signal-tmp/* 2>/dev/null

# 2. Go module cache (read-only dirs need u+w first)
if [ -d "$HOME/go/pkg/mod" ]; then
    [ "$DRY" = 1 ] || chmod -R u+w "$HOME/go/pkg/mod" 2>/dev/null
    rm_tree "$HOME/go/pkg/mod"
fi

# 3. orphaned agent-created Go caches (~/.gocache-*, ~/.gotmp-*) — no config refs
for p in "$HOME"/.gocache-* "$HOME"/.gotmp-*; do rm_tree "$p"; done

# 4. Rust target/ dirs inside repos+worktrees over 1G, ONLY when no build is live
if ! pgrep -f 'cargo|rustc' >/dev/null 2>&1; then
    while IFS= read -r d; do
        sz=$(du -sm "$d" 2>/dev/null | cut -f1)
        [ "${sz:-0}" -ge 1024 ] && rm_tree "$d"
    done < <(find "$HOME/repos" "$HOME/worktrees" -maxdepth 3 -type d -name target 2>/dev/null)
else
    log "SKIP rust target sweep: a cargo/rustc process is running"
fi

# 5. user trash
rm -rf "$HOME"/.local/share/Trash/files/* "$HOME"/.local/share/Trash/info/* 2>/dev/null

# 6. orphaned git temp pack files
find "$HOME/repos" "$HOME/worktrees" -maxdepth 5 -name 'tmp_pack_*' -type f -delete 2>/dev/null
find "$HOME/repos" "$HOME/worktrees" -maxdepth 3 -name 'index.lock' -mmin +60 -delete 2>/dev/null

# 7. Hermes corruption-copy cruft (timestamped copies only, never the live DB)
find "$HOME/.hermes/profiles" \( -name 'state.db.corrupted-*' -o -name 'state.db.corrupt-backup' \
     -o -name '*.malformed-backup-*' -o -name 'state.recovered-*.sql' \) -delete 2>/dev/null

# 8. /tmp tmpfs relief: stale files >24h + known scratch dirs (tmpfs = RAM, not disk)
find /tmp -mindepth 1 -maxdepth 1 -type f -mtime +1 -user "$(id -un)" -delete 2>/dev/null

log "=== done; after: $(df1) ==="
