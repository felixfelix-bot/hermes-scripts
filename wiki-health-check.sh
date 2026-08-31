#!/usr/bin/env bash
# OKF Wiki health check — runs as cron, silent on success, alerts on issues
# Checks: broken links, orphan pages, index completeness, stale timestamps
set -uo pipefail

WIKI="${WIKI_PATH:-$HOME/wiki}"
ISSUES=0
REPORT=""
BROKEN_LINKS=""

# 1. Check all concept files have valid frontmatter
for f in "$WIKI"/repos/*.md "$WIKI"/infrastructure/*.md "$WIKI"/playbooks/*.md; do
    [ -f "$f" ] || continue
    if ! head -1 "$f" | grep -q '^---'; then
        REPORT+="FRONTMATTER: $(basename "$f") missing opening ---\n"
        ISSUES=$((ISSUES + 1))
    fi
    if ! grep -q '^type:' "$f"; then
        REPORT+="FRONTMATTER: $(basename "$f") missing type: field\n"
        ISSUES=$((ISSUES + 1))
    fi
done

# 2. Check broken markdown links (relative only) — collect into var, no pipe
for f in "$WIKI"/repos/*.md "$WIKI"/infrastructure/*.md "$WIKI"/playbooks/*.md; do
    [ -f "$f" ] || continue
    while IFS= read -r link; do
        [ -z "$link" ] && continue
        [[ "$link" == http* ]] && continue
        target="$(dirname "$f")/$link"
        if [ ! -f "$target" ]; then
            BROKEN_LINKS+="BROKEN_LINK: $(basename "$f") -> $link\n"
        fi
    done < <(grep -oP '\[.*?\]\(\K[^)]+\.md' "$f" 2>/dev/null)
done

if [ -n "$BROKEN_LINKS" ]; then
    REPORT+="$BROKEN_LINKS"
    # Count broken links
    BL_COUNT=$(echo -e "$BROKEN_LINKS" | grep -c "BROKEN_LINK" || true)
    ISSUES=$((ISSUES + BL_COUNT))
fi

# 3. Check index.md references all concept files
# Count only references to repos/, infrastructure/, playbooks/ (not sub-indexes)
CONCEPT_COUNT=$(find "$WIKI"/repos "$WIKI"/infrastructure "$WIKI"/playbooks -name '*.md' 2>/dev/null | wc -l)
INDEX_CONCEPT_REFS=$(grep -cP '\((repos|infrastructure|playbooks)/' "$WIKI/index.md" 2>/dev/null || echo 0)
if [ "$CONCEPT_COUNT" -ne "$INDEX_CONCEPT_REFS" ]; then
    REPORT+="INDEX_MISMATCH: $CONCEPT_COUNT concept files but $INDEX_CONCEPT_REFS concept refs in index.md\n"
    ISSUES=$((ISSUES + 1))
fi

# 4. Check for stale files (timestamp > 90 days old)
CUTOFF=$(date -d '90 days ago' -u +%Y-%m-%d 2>/dev/null || echo "")
STALE_COUNT=0
if [ -n "$CUTOFF" ]; then
    for f in "$WIKI"/repos/*.md "$WIKI"/infrastructure/*.md "$WIKI"/playbooks/*.md; do
        [ -f "$f" ] || continue
        ts=$(grep -oP '^timestamp:\s*\K[0-9]{4}-[0-9]{2}-[0-9]{2}' "$f" 2>/dev/null | head -1)
        if [ -n "$ts" ] && [[ "$ts" < "$CUTOFF" ]]; then
            REPORT+="STALE: $(basename "$f") last updated $ts\n"
            STALE_COUNT=$((STALE_COUNT + 1))
        fi
    done
    ISSUES=$((ISSUES + STALE_COUNT))
fi

# 5. Check sub-indexes exist
for sub in tollgate microfips market firmware soveng infra; do
    if [ ! -f "$WIKI/${sub}-index.md" ]; then
        REPORT+="MISSING_SUBINDEX: ${sub}-index.md not found\n"
        ISSUES=$((ISSUES + 1))
    fi
done

# 6. Check git clean (uncommitted changes mean wiki is out of sync)
if [ -d "$WIKI/.git" ]; then
    UNCOMMITTED=$(cd "$WIKI" && git status --porcelain 2>/dev/null | wc -l)
    if [ "$UNCOMMITTED" -gt 0 ]; then
        REPORT+="GIT_DIRTY: $UNCOMMITTED uncommitted files in wiki repo\n"
        ISSUES=$((ISSUES + 1))
    fi
fi

# Output
if [ "$ISSUES" -gt 0 ]; then
    echo "WIKI HEALTH CHECK: $ISSUES issues found"
    echo -e "$REPORT"
    exit 1
else
    # Silent on success
    exit 0
fi
