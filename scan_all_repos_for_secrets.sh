#!/usr/bin/env bash
# scan_all_repos_for_secrets.sh — Scan ALL git repos for leaked API keys
#
# Scans working trees for known secret patterns.
# Skips ephemeral paths (/tmp), Rust build artifacts, and allowlisted test keys.
# Lines containing "# EPHEMERAL-KEY" are skipped (generated test keys).
#
# PASS 1 (class patterns)  — regex shapes: nsec1…, sk-…, 64-hex-with-keyword, PEM…
# PASS 2 (literal gate)    — CRED-H3/H7: the exact retired/rotated credential
#   literals in ~/.git-hooks/cred-needles.txt, via ~/.git-hooks/cred_gate.sh.
#   A shape scan cannot catch a value that is already known-leaked: it must be
#   matched literally.  Pass 2 checks both the working tree on disk AND each
#   repo's committed HEAD tree, because the dangerous state is a scrubbed working
#   tree on a branch whose commit still carries the value (the re-publish path).
#   Pass 2 is fail-closed: if the gate or its needle table is unusable, that is
#   reported as a violation, never silently skipped.
#
# Usage:
#   ./scan_all_repos_for_secrets.sh            # scan + report
#   ./scan_all_repos_for_secrets.sh --notify   # scan + report + Signal alert
#
# Ops / test knobs (optional; defaults are the full nightly behaviour):
#   SECRET_SCAN_REPORT=<path>  report file      (default /tmp/secret-scan-report.md)
#   SECRET_SCAN_LOCK=<path>    flock file       (default ~/.tmp/secret-scan.lock)
#   SCAN_FILTER=<substr>       scan only repos whose full path contains <substr>
#   SCAN_LIMIT=<n>             stop after n repos (after filtering; 0 = all)
#   SCAN_TIMEOUT=<sec>         per-repo cap for each grep/gate call (default 180)
#   SCAN_DEADLINE=<sec>        whole-run cap (default 10800 = 3h).  On expiry the
#                              run stops, STILL writes the report, and exits 1
#                              marked INCOMPLETE (fail-closed: silent truncation
#                              of coverage is itself a finding).
#   SCAN_NO_PRUNE=1            do not prune heavy dirs during repo discovery
#
# Runtime notes (measured on the live fleet 2026-09-18, host load ~16):
#   * Repo discovery must prune node_modules/.cache/snap/… — without pruning the
#     walk alone cost ~40s and descended into dependency caches that can hold no
#     tracked repo we care about.
#   * PASS 1 is the dominant cost, so it runs grep -I: a known credential literal
#     is ASCII text, and scanning binaries only adds page-cache churn (31.6s ->
#     10.5s on hermes-orchestration).
#   * Every per-repo call is capped by SCAN_TIMEOUT, so one pathological repo can
#     no longer stall the whole nightly run (previous behaviour: >2h25m without
#     finishing and no report).
#   * A single-instance flock stops the 03:00 cron from stacking a second run on
#     top of a slow one.
#
# Cron: runs nightly at 3am
set -euo pipefail

HOME_DIR="/home/c03rad0r"
REPORT="${SECRET_SCAN_REPORT:-/tmp/secret-scan-report.md}"
ALLOWLIST_FILE="$HOME_DIR/.secret-scan-allowlist.txt"
LOCK_FILE="${SECRET_SCAN_LOCK:-$HOME_DIR/.tmp/secret-scan.lock}"
SCAN_FILTER="${SCAN_FILTER:-}"
SCAN_LIMIT="${SCAN_LIMIT:-0}"
SCAN_TIMEOUT="${SCAN_TIMEOUT:-180}"
SCAN_DEADLINE="${SCAN_DEADLINE:-10800}"
SCAN_NO_PRUNE="${SCAN_NO_PRUNE:-0}"
START_SECONDS=$SECONDS

# Deployment paths.  The fixture harness (tests/test-litscan.sh) rewrites the
# HOME_DIR line above and symlinks this directory, so everything below stays
# valid in a fixture home.
LIT_GATE="$HOME_DIR/.git-hooks/cred_gate.sh"
LIT_NEEDLES="$HOME_DIR/.git-hooks/cred-needles.txt"
REDACTOR="$HOME_DIR/.git-hooks/redact-needles.py"

# Patterns to scan
PATTERNS=(
    'nsec1[a-z0-9]{20,}'
    'sk-or-v1-[a-z0-9]{20,}'
    'sk-[A-Za-z0-9]{10,}'
    '[0-9a-f]{32}\.[A-Za-z0-9]{8,}'
    'ghp_[A-Za-z0-9]{30,}'
    'syt\.[A-Za-z0-9_-]{10,}'
    '-----BEGIN [A-Z ]*PRIVATE KEY'
    # §22.5: keyword-anchored 64-hex (bare checksums/txids/public keys excluded)
    '(?i)(nsec[_ -]?hex|private[_ -]?key|mnemonic|secret[_ -]?key)[^\n:=\x60]{0,15}?[:=]\s*[\x22\x27\x60 ]?[0-9a-f]{64}\b'
)

# Dirs to skip entirely (regex matched against full repo path)
SKIP_DIRS=".git/node_modules|/worktrees/|hermes-orchestration.bak|backup-garbled|/\.venv/|/__pycache__/|/lsp/node_modules/|\.nsite/|/target/|\.fingerprint/|/dist/|/build/|/.next/|/.cache/"

# Build allowlist grep -v pattern from file
ALLOWLIST_GREP=""
if [ -f "$ALLOWLIST_FILE" ]; then
    ALLOWLIST_GREP=$(grep -v '^#' "$ALLOWLIST_FILE" 2>/dev/null | grep -v '^$' | paste -sd'|' -)
fi

# Build combined grep pattern
COMBINED=$(IFS='|'; echo "${PATTERNS[*]}")

echo "Scanning all git repos under $HOME_DIR for secrets..."
echo "Started: $(date)"
echo "Allowlist: ${ALLOWLIST_FILE:-none}"
echo "Report: $REPORT"
if [ -n "$SCAN_FILTER" ]; then echo "Filter: $SCAN_FILTER"; fi
if [ "$SCAN_LIMIT" -gt 0 ]; then echo "Limit: $SCAN_LIMIT repos"; fi
echo "Per-repo timeout: ${SCAN_TIMEOUT}s   Whole-run deadline: ${SCAN_DEADLINE}s"

# ---------------------------------------------------------------------------
# Single-instance guard: a slow run must never be joined by the next nightly one
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "another secret scan already holds $LOCK_FILE — exiting without scanning"
        exit 0
    fi
    echo "Lock acquired: $LOCK_FILE"
fi

# ---------------------------------------------------------------------------
# Repo discovery (once, pruned) — both passes iterate the same list
# ---------------------------------------------------------------------------
discover_repos() {
    if [ "$SCAN_NO_PRUNE" = "1" ]; then
        find "$HOME_DIR" -maxdepth 5 -name .git -type d 2>/dev/null
    else
        find "$HOME_DIR" -maxdepth 5 \
            \( -name node_modules -o -name .cache -o -name .venv -o -name snap \
               -o -name .cargo -o -name .rustup -o -name .npm -o -name .local \
               -o -name .mozilla -o -name .var \) -prune -o \
            -name .git -type d -print 2>/dev/null
    fi
}

REPO_LIST=$(discover_repos | sed 's|/\.git$||' | sort -u || true)

# selected <repo> — the one filter both passes and the tally share.
selected() {
    if printf '%s' "$1" | grep -qE "$SKIP_DIRS"; then return 1; fi
    if [ -n "$SCAN_FILTER" ]; then
        case "$1" in
            *"$SCAN_FILTER"*) ;;
            *) return 1 ;;
        esac
    fi
    return 0
}

# budget_out <pass> <index> <total> — true once SCAN_LIMIT/SCAN_DEADLINE is spent.
budget_out() {
    if [ "$SCAN_LIMIT" -gt 0 ] && [ "$2" -gt "$SCAN_LIMIT" ]; then
        INCOMPLETE=1; DEADLINE_HIT="SCAN_LIMIT=$SCAN_LIMIT reached"
        echo "LIMIT: $1 pass stopped after $(($2 - 1)) repos (SCAN_LIMIT=$SCAN_LIMIT)"
        return 0
    fi
    if [ "$SCAN_DEADLINE" -gt 0 ] && [ $((SECONDS - START_SECONDS)) -ge "$SCAN_DEADLINE" ]; then
        INCOMPLETE=1; DEADLINE_HIT="SCAN_DEADLINE=${SCAN_DEADLINE}s reached"
        echo "DEADLINE: $1 pass stopped after $(($2 - 1))/$3 repos ($(date))"
        return 0
    fi
    return 1
}

TOTAL=0
while IFS= read -r r; do
    [ -n "$r" ] || continue
    selected "$r" || continue
    TOTAL=$((TOTAL + 1))
done <<< "$REPO_LIST"
echo "Repos discovered: $(printf '%s\n' "$REPO_LIST" | grep -c . || true) — selected for scanning: $TOTAL"
echo ""

# run_capped <secs> <cmd...> — run with a per-repo cap when `timeout` is available
run_capped() {
    local secs="$1"; shift
    if [ "$secs" -gt 0 ] && command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    else
        "$@"
    fi
}

VIOLATIONS=0
REPOS_SCANNED=0
INCOMPLETE=0
DEADLINE_HIT=""
RESULTS=""
idx=0

# Find all git repos (max depth 5)
while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    selected "$repo" || continue

    idx=$((idx + 1))
    if budget_out class "$idx" "$TOTAL"; then break; fi

    REPOS_SCANNED=$((REPOS_SCANNED + 1))
    printf '[%d/%d] %s\n' "$idx" "$TOTAL" "$repo"

    # Scan working tree only (tracked + untracked files)
    # Excludes: build artifacts, caches, databases, ephemeral paths
    # -I: a credential literal is text; skipping binaries avoids page-cache churn
    wt_hits=$(cd "$repo" && run_capped "$SCAN_TIMEOUT" grep -rnIP \
        --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir=".venv" \
        --exclude-dir="__pycache__" --exclude-dir="target" --exclude-dir="dist" \
        --exclude-dir="build" --exclude-dir=".next" --exclude-dir=".cache" \
        --exclude="*.pyc" --exclude="*.lock" --exclude="*.db" \
        --exclude="*.db-wal" --exclude="*.db-shm" --exclude="*.min.js" \
        --exclude="*.map" --exclude="package-lock.json" --exclude="bun.lock" \
        "$COMBINED" . 2>/dev/null \
        | grep -v "EPHEMERAL-KEY" \
        | awk 'NR<=20' || true)

    # Apply allowlist if present
    if [ -n "$ALLOWLIST_GREP" ] && [ -n "$wt_hits" ]; then
        wt_hits=$(echo "$wt_hits" | grep -vE "$ALLOWLIST_GREP" || true)
    fi

    if [ -n "$wt_hits" ]; then
        reponame=$(basename "$repo")
        count=$(echo "$wt_hits" | wc -l)
        VIOLATIONS=$((VIOLATIONS + count))
        # Never paste the raw grep line into the report: it carries the matched
        # value.  Redact through the needle table (fail-closed: drop the text
        # entirely if the redactor is unusable).
        excerpt=$(printf '%s\n' "$wt_hits" | awk 'NR<=5')
        red_excerpt=""
        if [ -x "$REDACTOR" ]; then
            red_excerpt=$(printf '%s\n' "$excerpt" | python3 "$REDACTOR" "$LIT_NEEDLES" 2>/dev/null) || red_excerpt=""
        fi
        if [ -z "$red_excerpt" ]; then
            red_excerpt=$(printf '%s\n' "$excerpt" | awk -F: 'NF>=3 {print $1 ":" $2 ": [matched text withheld — redactor unavailable]"}')
        fi
        RESULTS="${RESULTS}\n### $reponame ($count hits)\n\`\`\`\n${red_excerpt}\n\`\`\`\n"
    fi

done <<< "$REPO_LIST"

# ---------------------------------------------------------------------------
# PASS 2 — CRED-H3/H7 retired-credential LITERAL gate (fail-closed)
# ---------------------------------------------------------------------------
LIT_REPOS=0
LIT_VIOLATIONS=0
LIT_RESULTS=""

if [ ! -x "$LIT_GATE" ] || [ ! -r "$LIT_NEEDLES" ]; then
    LIT_VIOLATIONS=1
    LIT_RESULTS="### CRED-GATE UNAVAILABLE (fail-closed) — literal scan did NOT run\n\`\`\`\ngate:    $LIT_GATE\ntable:   $LIT_NEEDLES\nRepair:  python3 $HOME_DIR/.git-hooks/build-cred-needles.py\n\`\`\`\n"
else
    idx=0
    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        selected "$repo" || continue

        idx=$((idx + 1))
        if budget_out literal "$idx" "$TOTAL"; then break; fi

        LIT_REPOS=$((LIT_REPOS + 1))
        printf '[lit %d/%d] %s\n' "$idx" "$TOTAL" "$repo"
        repo_hits=""
        unusable=""

        # (a) files on disk (working tree)
        rc=0
        out=$(cd "$repo" && run_capped "$SCAN_TIMEOUT" "$LIT_GATE" --path . --quiet 2>&1) || rc=$?
        case "$rc" in
            0) ;;
            1) repo_hits="${repo_hits}${out}\n" ;;
            *) unusable="${unusable}[working tree] rc=$rc ${out}\n" ;;
        esac

        # (b) committed HEAD tree — the re-publish path
        if git -C "$repo" rev-parse --verify -q HEAD >/dev/null 2>&1; then
            rc=0
            out=$(cd "$repo" && run_capped "$SCAN_TIMEOUT" "$LIT_GATE" --tree HEAD --quiet 2>&1) || rc=$?
            case "$rc" in
                0) ;;
                1) repo_hits="${repo_hits}${out}\n" ;;
                *) unusable="${unusable}[committed HEAD] rc=$rc ${out}\n" ;;
            esac
        fi

        if [ -n "$repo_hits" ]; then
            reponame=$(basename "$repo")
            n=$(printf '%b' "$repo_hits" | grep -c 'CRED-GATE: retired credential literal present' || true)
            LIT_VIOLATIONS=$((LIT_VIOLATIONS + n))
            LIT_RESULTS="${LIT_RESULTS}\n### $reponame — retired credential literal (${n} finding(s))\n\`\`\`\n$(printf '%b' "$repo_hits" | awk 'NR<=12')\n\`\`\`\n"
        fi
        if [ -n "$unusable" ]; then
            reponame=$(basename "$repo")
            LIT_VIOLATIONS=$((LIT_VIOLATIONS + 1))
            LIT_RESULTS="${LIT_RESULTS}\n### $reponame — cred-gate UNUSABLE (fail-closed)\n\`\`\`\n${unusable}\`\`\`\n"
        fi
    done <<< "$REPO_LIST"
fi

# ---------------------------------------------------------------------------
# PASS 3 — wallet material: REAL BIP-39 seeds + Cashu bearer tokens
#          (pre-filter to seed-ish files, then validate; findings are masked)
# ---------------------------------------------------------------------------
DETECT_WALLET="$HOME_DIR/.git-hooks/detect_wallet_secrets.py"
if [ -f "$DETECT_WALLET" ]; then
    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        selected "$repo" || continue
        if [ -z "${SCAN_FILTER:-}" ] && budget_out class "$REPOS_SCANNED" "$TOTAL"; then break; fi
        cand=$( (cd "$repo" && run_capped "$SCAN_TIMEOUT" grep -rlI -E             --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir=".venv"             --exclude-dir="__pycache__" --exclude-dir="target" --exclude-dir="dist"             --exclude-dir="build" --exclude="*.min.js" --exclude="*.map"             'mnemonic|seed[ _-]?phrase|recovery phrase|cashu[AB][A-Za-z0-9_+/=-]{40,}' . 2>/dev/null)             | awk 'NR<=200' || true)
        [ -n "$cand" ] || continue
        ws_hits=$(printf '%s\n' "$cand" | while IFS= read -r f; do
            python3 "$DETECT_WALLET" "$repo/$f" 2>/dev/null
        done)
        [ -n "$ws_hits" ] || continue
        ws_count=$(printf '%s\n' "$ws_hits" | grep -c 'WALLET-SECRET' || true)
        [ "$ws_count" -gt 0 ] || continue
        VIOLATIONS=$((VIOLATIONS + ws_count))
        reponame=$(basename "$repo")
        RESULTS="${RESULTS}\n### $reponame ($ws_count wallet-material finding(s))\n\`\`\`\n$(printf '%s\n' "$ws_hits" | grep 'WALLET-SECRET' | awk 'NR<=10')\n\`\`\`\n"
    done <<< "$REPO_LIST"
fi

# Generate report
{
    echo "# Secret Scan Report"
    echo ""
    echo "- **Date**: $(date)"
    echo "- **Repos scanned**: $REPOS_SCANNED (class patterns) / $LIT_REPOS (literal gate)"
    echo "- **Coverage**: $(if [ "$INCOMPLETE" -eq 1 ]; then echo "⚠️ INCOMPLETE — ${DEADLINE_HIT}"; else echo "complete ($TOTAL repos selected)"; fi)"
    echo "- **Duration**: $((SECONDS - START_SECONDS))s"
    echo "- **Violations found**: $((VIOLATIONS + LIT_VIOLATIONS)) (class: $VIOLATIONS, literal-gate: $LIT_VIOLATIONS)"
    echo "- **Allowlist**: ${ALLOWLIST_FILE:-none}"
    echo ""
    if [ "$INCOMPLETE" -eq 1 ] && [ "$((VIOLATIONS + LIT_VIOLATIONS))" -eq 0 ]; then
        echo "⚠️ **Coverage incomplete (${DEADLINE_HIT}) — absence of findings is NOT proof of absence.**"
        echo ""
    fi
    if [ "$((VIOLATIONS + LIT_VIOLATIONS))" -eq 0 ]; then
        echo "✅ **No secrets detected.** All repos are clean."
    else
        echo "⚠️ **SECRETS DETECTED — review below.**"
        echo ""
        if [ "$LIT_VIOLATIONS" -gt 0 ]; then
            echo "## Retired-credential literal gate (CRED-H3/H7)"
            echo ""
            echo "- Needle table: $LIT_NEEDLES"
            echo "- Literals are never printed; findings carry the rule id + a sha256/12 fingerprint."
            echo -e "$LIT_RESULTS"
        fi
        if [ "$VIOLATIONS" -gt 0 ]; then
            echo "## Class-pattern scan"
            echo ""
            echo -e "$RESULTS"
        fi
    fi
} > "$REPORT"

echo ""
echo "=== Scan complete ==="
echo "Repos scanned: $REPOS_SCANNED (class) / $LIT_REPOS (literal gate)"
echo "Violations: $((VIOLATIONS + LIT_VIOLATIONS)) (class: $VIOLATIONS, literal-gate: $LIT_VIOLATIONS)"
echo "Coverage: $(if [ "$INCOMPLETE" -eq 1 ]; then echo "INCOMPLETE — ${DEADLINE_HIT}"; else echo complete; fi)"
echo "Report: $REPORT"

# Notify via Signal if --notify flag and anything needs attention
NOTIFY=0
for a in "$@"; do [ "$a" = "--notify" ] && NOTIFY=1; done
if [ "$NOTIFY" -eq 1 ] && { [ "$((VIOLATIONS + LIT_VIOLATIONS))" -gt 0 ] || [ "$INCOMPLETE" -eq 1 ]; }; then
    if [ "$INCOMPLETE" -eq 1 ]; then
        SUMMARY="⚠️ Secret scan INCOMPLETE (${DEADLINE_HIT}) after $REPOS_SCANNED/$TOTAL repos — $((VIOLATIONS + LIT_VIOLATIONS)) violation(s) so far. See $REPORT."
    else
        SUMMARY="⚠️ Secret scan found $((VIOLATIONS + LIT_VIOLATIONS)) violations across $REPOS_SCANNED repos ($LIT_VIOLATIONS of them retired-credential literals). See $REPORT for details."
    fi
    curl -s -X POST http://localhost:8080/api/v1/rpc \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"method\":\"send\",\"params\":{\"message\":\"$SUMMARY\"},\"id\":1}" \
        2>/dev/null || true
    echo "Notification sent."
fi

EXIT=0
[ "$((VIOLATIONS + LIT_VIOLATIONS))" -gt 0 ] && EXIT=1
[ "$INCOMPLETE" -eq 1 ] && EXIT=1
exit "$EXIT"
