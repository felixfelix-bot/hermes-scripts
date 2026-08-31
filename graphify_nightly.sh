#!/usr/bin/env bash
# graphify_nightly — refresh knowledge graphs for actively-developed repos.
# Uses AST extraction (free) + optional z.ai proxy for semantic pass.
# Zero tokens if OPENAI_API_KEY is unset (AST-only mode).
set -u
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:9099/v1}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434/v1}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:3b}"

# Repos to index (actively developed, skip dormant)
ACTIVE_REPOS=(
  "$HOME/repos/tollgate-rs-spilman"
  "$HOME/repos/org-scan/plebeian-market"
  "$HOME/repos/balloon-fresh"
)

# Add repos from repos.txt that have recent git activity (last 7 days)
if [ -f "$HOME/.hermes/bot/repos.txt" ]; then
  while IFS= read -r repo; do
    [ -z "$repo" ] && continue
    [ -d "$repo" ] || continue
    # Check for recent commits
    if git -C "$repo" log --oneline --since="7 days ago" 2>/dev/null | grep -q .; then
      ACTIVE_REPOS+=("$repo")
    fi
  done < <(grep -v '^#' "$HOME/.hermes/bot/repos.txt" 2>/dev/null)
fi

# Dedupe
mapfile -t ACTIVE_REPOS < <(printf '%s\n' "${ACTIVE_REPOS[@]}" | sort -u)

updated=0
failed=0
for repo in "${ACTIVE_REPOS[@]}"; do
  [ -d "$repo" ] || continue
  echo "[$(basename "$repo")] refreshing graph..."
  if [ -d "$repo/graphify-out" ]; then
    # Incremental update — only changed files
    if graphify "$repo" --update --backend openai 2>/dev/null; then
      updated=$((updated + 1))
    else
      echo "  → update failed, trying AST-only"
      graphify "$repo" --update 2>/dev/null && updated=$((updated + 1)) || failed=$((failed + 1))
    fi
  else
    # Full build — AST + semantic
    if graphify "$repo" --backend openai 2>/dev/null; then
      updated=$((updated + 1))
    else
      echo "  → semantic failed, AST-only fallback"
      graphify "$repo" 2>/dev/null && updated=$((updated + 1)) || failed=$((failed + 1))
    fi
  fi
done

echo "graphify-nightly: $updated updated, $failed failed, ${#ACTIVE_REPOS[@]} repos total"
