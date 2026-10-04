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
#   SCAN_HOME_SWEEP=1          ALSO sweep $HOME itself, as a FINAL entry, for
#                              residue OUTSIDE every repo — a plaintext note in ~/
#                              or ~/secrets/ is visible to nothing else, and that
#                              is how the 2026-09-18 dq05 login note was found.
#                              Off by default: the entry walks ~/worktrees and
#                              every sibling tree, so it is the most expensive one
#                              in the run and normally the one that hits its cap.
#   SCAN_HOME_SWEEP_TIMEOUT=<sec>  per-call cap for that one entry (default 900)
#
# Discovery invariant: a `.git` DIRECTORY alone does not make a repo.  A candidate
# counts as a repo only when `git -C <dir> rev-parse --git-dir` accepts it; that
# also accepts a 0-commit `git init` tree, whose working files still need scanning.
# A candidate git REJECTS gets one of two verdicts — both printed and both listed
# in the report, never silently dropped:
#   * `not-a-repo`  — it is an ANCESTOR of other scan targets, so its tree IS
#                     those entries.  /home/c03rad0r/.git was exactly this (a dir
#                     holding only hooks/): discovery made the WHOLE home
#                     directory "repo 1", costing ~224s of the nightly sweep
#                     (PASS 1 capped at 180s, and its truncated grep output
#                     reported as if it were complete) before one of the 430 real
#                     repos was reached.  Whole-home coverage is now the explicit,
#                     documented SCAN_HOME_SWEEP above, never a side effect.
#   * `damaged-git` — it is not an ancestor, so its WORKING TREE is still a place
#                     a credential can sit: it stays in the scan (PASS 1 + the
#                     literal gate on disk); only the committed-HEAD pass is
#                     impossible, because git has no HEAD to give.  Dropping these
#                     would silently remove coverage the pre-fix scanner had for
#                     those trees.  The live fleet has 30 such paths (a mutilated
#                     .git with no HEAD, e.g. ~/repos/coinos, ~/Downloads/tollgate).
#
# Truncation invariant: a capped per-repo call can never look complete.  A PASS 1
# grep killed by the per-repo cap (rc=124/137) is surfaced as `truncated-p1
# <entry>`, listed under "Coverage gaps" in the report, and marks the run
# INCOMPLETE (exit 1) — the same way PASS 2 already fails closed on rc>=2.  Same
# treatment for PASS 3 (`truncated-p3`) and for a non-benign grep error
# (`p1-error`); benign read skips (vanished file, EACCES, symlink loop) are
# counted and reported without pretending the entry was clean.
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
SCAN_HOME_SWEEP="${SCAN_HOME_SWEEP:-0}"
SCAN_HOME_SWEEP_TIMEOUT="${SCAN_HOME_SWEEP_TIMEOUT:-900}"
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

# Files excluded from PASS 1 — the gate's OWN credential tables.  They hold the
# retired literals BY DESIGN (they are the needle source), so a whole-home sweep
# would otherwise report its own table on every run.  This mirrors cred_gate.sh's
# own default excludes; the tables are audited by the gate's harnesses instead.
GATE_TABLE_EXCLUDES=( --exclude="cred-needles.txt" --exclude="cred-scan-rules.json"
                      --exclude="cred-needles-extra.txt" --exclude="cred-exempt.txt" )

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
# Repo discovery (once, pruned) — every pass iterates the same validated list
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

# is_valid_repo <dir> — see "Discovery invariant" in the header.  Without git on
# PATH we keep the pre-fix behaviour (scan the candidate) rather than drop repos.
is_valid_repo() {
    command -v git >/dev/null 2>&1 || return 0
    git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

# now_ms — elapsed-time reporting (bash >= 5 EPOCHREALTIME, else 1s granularity).
now_ms() {
    if [ -n "${EPOCHREALTIME:-}" ]; then
        local t="${EPOCHREALTIME/./}"
        printf '%s' "$(( ${t:0:${#t}-3} ))"
    else
        printf '%s' "$(( SECONDS * 1000 ))"
    fi
}

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

# ---------------------------------------------------------------------------
# Selection — validated repos in discovery order, then (opt-in) the home sweep
# entry LAST, so the most expensive entry can never starve the real repos the way
# the 224s malformed-~/.git entry did when it was entry #1 of 430.
# ---------------------------------------------------------------------------
SCAN_LIST=""
NOT_REPO_LIST=""
DAMAGED_LIST=""
TOTAL=0
HOME_SWEEP_ENTRY="0"

while IFS= read -r r; do
    [ -n "$r" ] || continue
    selected "$r" || continue
    if ! is_valid_repo "$r"; then
        # Ancestor test (literal, not a regex): does any discovered .git dir live
        # UNDER this candidate?  If so its tree IS those entries — scanning it
        # would re-scan them (and, for $HOME, the whole fleet) — so it is a
        # `not-a-repo` skip.  Otherwise its working tree is still scanned as a
        # `damaged-git` entry, which preserves the pre-fix coverage of that tree.
        if printf '%s' "$REPO_LIST" | awk -v p="$r/" 'index($0, p) == 1 {f=1} END {exit !f}'; then
            NOT_REPO_LIST="${NOT_REPO_LIST}${r}"$'\n'
            continue
        fi
        DAMAGED_LIST="${DAMAGED_LIST}${r}"$'\n'
    fi
    SCAN_LIST="${SCAN_LIST}${r}"$'\n'
    TOTAL=$((TOTAL + 1))
done <<< "$REPO_LIST"

if [ "$SCAN_HOME_SWEEP" = "1" ] && selected "$HOME_DIR" \
   && ! printf '%s' "$SCAN_LIST" | grep -qxF "$HOME_DIR"; then
    SCAN_LIST="${SCAN_LIST}${HOME_DIR}"$'\n'
    TOTAL=$((TOTAL + 1))
    HOME_SWEEP_ENTRY="1"
fi
NOT_REPO_COUNT=$(printf '%s' "$NOT_REPO_LIST" | grep -c . || true)
DAMAGED_COUNT=$(printf '%s' "$DAMAGED_LIST" | grep -c . || true)

echo "Repos discovered: $(printf '%s\n' "$REPO_LIST" | grep -c . || true) — entries selected for scanning: $TOTAL"
if [ "$HOME_SWEEP_ENTRY" = "1" ]; then
    echo "Whole-home sweep: ON — $HOME_DIR is scanned LAST for residue outside every repo (cap ${SCAN_HOME_SWEEP_TIMEOUT}s)"
fi
if [ "$NOT_REPO_COUNT" -gt 0 ]; then
    echo "not-a-repo (a .git git rejects AND an ancestor of other scan targets — SKIPPED, not scanned): $NOT_REPO_COUNT"
    printf '%b' "$NOT_REPO_LIST" | sed 's|^|  not-a-repo |'
fi
if [ "$DAMAGED_COUNT" -gt 0 ]; then
    echo "damaged-git (a .git git rejects — working tree IS scanned, no committed-HEAD pass): $DAMAGED_COUNT"
    printf '%b' "$DAMAGED_LIST" | sed 's|^|  damaged-git |'
fi
echo ""

# note_incomplete <reason> — ONE place decides what "coverage" means, so the
# report, the Signal message and the exit code cannot disagree.
INCOMPLETE=0
INCOMPLETE_REASONS=""
note_incomplete() {
    INCOMPLETE=1
    if [ -n "$INCOMPLETE_REASONS" ]; then
        INCOMPLETE_REASONS="${INCOMPLETE_REASONS}; $1"
    else
        INCOMPLETE_REASONS="$1"
    fi
}

# budget_out <pass> <index> <total> — true once SCAN_LIMIT/SCAN_DEADLINE is spent.
budget_out() {
    if [ "$SCAN_LIMIT" -gt 0 ] && [ "$2" -gt "$SCAN_LIMIT" ]; then
        echo "LIMIT: $1 pass stopped after $(($2 - 1)) repos (SCAN_LIMIT=$SCAN_LIMIT)"
        note_incomplete "SCAN_LIMIT=$SCAN_LIMIT reached in the $1 pass"
        return 0
    fi
    if [ "$SCAN_DEADLINE" -gt 0 ] && [ $((SECONDS - START_SECONDS)) -ge "$SCAN_DEADLINE" ]; then
        echo "DEADLINE: $1 pass stopped after $(($2 - 1))/$3 repos ($(date))"
        note_incomplete "SCAN_DEADLINE=${SCAN_DEADLINE}s reached in the $1 pass"
        return 0
    fi
    return 1
}

# redact_stream <text> — presentation layer for ANY text that reaches the report.
# The report lands on disk, in the cron log and in a Signal message, so it must
# never carry credential material — including a credential-shaped FILE NAME.
# Fail-closed: if the redactor is unusable the text is dropped entirely.
redact_stream() {
    [ -n "${1:-}" ] || return 0
    if [ -x "$REDACTOR" ]; then
        if printf '%s\n' "$1" | python3 "$REDACTOR" "$LIT_NEEDLES" 2>/dev/null; then
            return 0
        fi
    fi
    printf '[withheld — redactor unavailable]\n'
}

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
RESULTS=""
P1_TRUNC=0
P1_ERRORS=0
P1_BENIGN=0
P1_RETCAP=0
TRUNC_LIST=""
P1_ERROR_LIST=""
TIMING_FILE=$(mktemp "${TMPDIR:-/tmp}/secret-scan-timings.XXXXXX")
P1_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/secret-scan-p1err.XXXXXX")
idx=0

# ---------------------------------------------------------------------------
# PASS 1 — class patterns over each entry's working tree
# ---------------------------------------------------------------------------
while IFS= read -r repo; do
    [ -n "$repo" ] || continue

    idx=$((idx + 1))
    if budget_out class "$idx" "$TOTAL"; then break; fi

    REPOS_SCANNED=$((REPOS_SCANNED + 1))
    printf '[%d/%d] %s\n' "$idx" "$TOTAL" "$repo"
    t0=$(now_ms)

    extra_excludes=()
    cap="$SCAN_TIMEOUT"
    if [ "$repo" = "$HOME_DIR" ]; then
        cap="$SCAN_HOME_SWEEP_TIMEOUT"
        extra_excludes=("${GATE_TABLE_EXCLUDES[@]}")
    fi

    # rc is captured, never discarded: 124/137 means the cap killed grep and the
    # output below is PARTIAL.  Previously the pipeline ended in `awk ... || true`
    # so a capped grep was indistinguishable from a clean repo.
    p1_rc=0
    wt_hits=$(cd "$repo" && run_capped "$cap" grep -rnIP \
        --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir=".venv" \
        --exclude-dir="__pycache__" --exclude-dir="target" --exclude-dir="dist" \
        --exclude-dir="build" --exclude-dir=".next" --exclude-dir=".cache" \
        --exclude="*.pyc" --exclude="*.lock" --exclude="*.db" \
        --exclude="*.db-wal" --exclude="*.db-shm" --exclude="*.min.js" \
        --exclude="*.map" --exclude="package-lock.json" --exclude="bun.lock" \
        ${extra_excludes[@]+"${extra_excludes[@]}"} \
        "$COMBINED" . 2>"$P1_ERR_FILE" \
        | grep -v "EPHEMERAL-KEY" \
        | awk 'NR<=20000') || p1_rc=$?

    # Count BEFORE capping what is displayed: the old `awk 'NR<=20'` sat inside the
    # substitution and silently became the tally, so a repo with 300 hits was
    # reported as 20.
    p1_count=0
    more=""
    capnote=""
    if [ -n "${wt_hits:-}" ]; then
        if [ -n "$ALLOWLIST_GREP" ]; then
            wt_hits=$(printf '%s\n' "$wt_hits" | grep -vE "$ALLOWLIST_GREP" || true)
        fi
        p1_count=$(printf '%s\n' "$wt_hits" | grep -c . || true)
    fi

    if [ -n "${wt_hits:-}" ]; then
        reponame=$(basename "$repo")
        VIOLATIONS=$((VIOLATIONS + p1_count))
        if [ "$p1_count" -gt 5 ]; then more=", first 5 shown"; fi
        if [ "$p1_count" -ge 20000 ]; then
            capnote=", retention cap 20000 reached — this tally is a LOWER BOUND"
        fi
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
        RESULTS="${RESULTS}\n### $reponame ($p1_count hits${more}${capnote})\n\`\`\`\n${red_excerpt}\n\`\`\`\n"
    fi

    case "$p1_rc" in
        0|1) ;;
        124|137)
            P1_TRUNC=$((P1_TRUNC + 1))
            TRUNC_LIST="${TRUNC_LIST}${repo} — PASS 1 grep hit the ${cap}s cap${capnote}"$'\n'
            printf 'truncated-p1 %s — grep hit the %ss cap; PASS 1 for this entry is INCOMPLETE (partial output only)\n' "$repo" "$cap"
            note_incomplete "PASS 1 truncated by the ${cap}s per-repo cap (${P1_TRUNC} entr(y/ies) so far)"
            ;;
        *)
            # grep rc>=2: an error, not a clean repo.  Benign read skips are
            # counted and reported; anything else marks coverage INCOMPLETE.
            if [ -s "$P1_ERR_FILE" ] && ! grep -qvE 'No such file or directory|Permission denied|Too many levels of symbolic links' "$P1_ERR_FILE"; then
                P1_BENIGN=$((P1_BENIGN + 1))
            else
                P1_ERRORS=$((P1_ERRORS + 1))
                P1_ERROR_LIST="${P1_ERROR_LIST}${repo} — grep rc=$p1_rc: $(head -1 "$P1_ERR_FILE")"$'\n'
                printf 'p1-error %s — grep rc=%s: %s\n' "$repo" "$p1_rc" "$(head -1 "$P1_ERR_FILE")"
                note_incomplete "PASS 1 grep error (rc=$p1_rc) in ${P1_ERRORS} entr(y/ies)"
            fi
            ;;
    esac

    if [ "$p1_count" -ge 20000 ]; then
        P1_RETCAP=$((P1_RETCAP + 1))
        note_incomplete "PASS 1 retention cap (20000 lines) reached in ${P1_RETCAP} entr(y/ies) — tallies are lower bounds"
    fi

    p1_ms=$(( $(now_ms) - t0 ))
    printf '%s\t%s\t%s\n' "$p1_ms" "$repo" "p1" >> "$TIMING_FILE"
    if [ "$p1_ms" -ge 2000 ]; then printf '        slow entry: %sms\n' "$p1_ms"; fi
done <<< "$SCAN_LIST"

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

        idx=$((idx + 1))
        if budget_out literal "$idx" "$TOTAL"; then break; fi

        LIT_REPOS=$((LIT_REPOS + 1))
        printf '[lit %d/%d] %s\n' "$idx" "$TOTAL" "$repo"
        t0=$(now_ms)
        lit_cap="$SCAN_TIMEOUT"
        if [ "$repo" = "$HOME_DIR" ]; then lit_cap="$SCAN_HOME_SWEEP_TIMEOUT"; fi
        repo_hits=""
        unusable=""

        # (a) files on disk (working tree)
        rc=0
        out=$(cd "$repo" && run_capped "$lit_cap" "$LIT_GATE" --path . --quiet 2>&1) || rc=$?
        case "$rc" in
            0) ;;
            1) repo_hits="${repo_hits}${out}\n" ;;
            124|137) unusable="${unusable}[working tree] TRUNCATED at the ${lit_cap}s cap (rc=$rc) — any finding below is partial\n" ;;
            *) unusable="${unusable}[working tree] rc=$rc ${out}\n" ;;
        esac

        # (b) committed HEAD tree — the re-publish path
        if git -C "$repo" rev-parse --verify -q HEAD >/dev/null 2>&1; then
            rc=0
            out=$(cd "$repo" && run_capped "$lit_cap" "$LIT_GATE" --tree HEAD --quiet 2>&1) || rc=$?
            case "$rc" in
                0) ;;
                1) repo_hits="${repo_hits}${out}\n" ;;
                124|137) unusable="${unusable}[committed HEAD] TRUNCATED at the ${lit_cap}s cap (rc=$rc) — any finding below is partial\n" ;;
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

        p2_ms=$(( $(now_ms) - t0 ))
        printf '%s\t%s\t%s\n' "$p2_ms" "$repo" "p2" >> "$TIMING_FILE"
        if [ "$p2_ms" -ge 2000 ]; then printf '        slow entry: %sms\n' "$p2_ms"; fi
    done <<< "$SCAN_LIST"
fi

# ---------------------------------------------------------------------------
# PASS 3 — wallet material: REAL BIP-39 seeds + Cashu bearer tokens
#          (pre-filter to seed-ish files, then validate; findings are masked)
# ---------------------------------------------------------------------------
P3_TRUNC=0
P3_TRUNC_LIST=""
DETECT_WALLET="$HOME_DIR/.git-hooks/detect_wallet_secrets.py"
if [ -f "$DETECT_WALLET" ]; then
    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        if [ -z "${SCAN_FILTER:-}" ] && budget_out class "$REPOS_SCANNED" "$TOTAL"; then break; fi
        p3_cap="$SCAN_TIMEOUT"
        if [ "$repo" = "$HOME_DIR" ]; then p3_cap="$SCAN_HOME_SWEEP_TIMEOUT"; fi
        p3_rc=0
        cand=$( (cd "$repo" && run_capped "$p3_cap" grep -rlI -E \
            --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir=".venv" \
            --exclude-dir="__pycache__" --exclude-dir="target" --exclude-dir="dist" \
            --exclude-dir="build" --exclude="*.min.js" --exclude="*.map" \
            'mnemonic|seed[ _-]?phrase|recovery phrase|cashu[AB][A-Za-z0-9_+/=-]{40,}' . 2>/dev/null) \
            | awk 'NR<=200' ) || p3_rc=$?
        if [ "$p3_rc" = "124" ] || [ "$p3_rc" = "137" ]; then
            P3_TRUNC=$((P3_TRUNC + 1))
            P3_TRUNC_LIST="${P3_TRUNC_LIST}${repo} — PASS 3 grep hit the ${p3_cap}s cap"$'\n'
            printf 'truncated-p3 %s — grep hit the %ss cap; the wallet-material pass is INCOMPLETE for this entry\n' "$repo" "$p3_cap"
            note_incomplete "PASS 3 truncated by the ${p3_cap}s per-repo cap (${P3_TRUNC} entr(y/ies) so far)"
        fi
        [ -n "$cand" ] || continue
        ws_hits=$(printf '%s\n' "$cand" | while IFS= read -r f; do
            python3 "$DETECT_WALLET" "$repo/$f" 2>/dev/null || true
        done)
        [ -n "$ws_hits" ] || continue
        ws_count=$(printf '%s\n' "$ws_hits" | grep -c 'WALLET-SECRET' || true)
        [ "$ws_count" -gt 0 ] || continue
        VIOLATIONS=$((VIOLATIONS + ws_count))
        reponame=$(basename "$repo")
        RESULTS="${RESULTS}\n### $reponame ($ws_count wallet-material finding(s))\n\`\`\`\n$(printf '%s\n' "$ws_hits" | grep 'WALLET-SECRET' | awk 'NR<=10')\n\`\`\`\n"
    done <<< "$SCAN_LIST"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
COVERAGE_GAPS=0
if [ "$P1_TRUNC" -gt 0 ] || [ "$P3_TRUNC" -gt 0 ] || [ "$P1_ERRORS" -gt 0 ] \
   || [ "$P1_RETCAP" -gt 0 ] || [ "$NOT_REPO_COUNT" -gt 0 ] || [ "$DAMAGED_COUNT" -gt 0 ]; then
    COVERAGE_GAPS=1
fi

{
    echo "# Secret Scan Report"
    echo ""
    echo "- **Date**: $(date)"
    echo "- **Repos scanned**: $REPOS_SCANNED (class patterns) / $LIT_REPOS (literal gate)"
    echo "- **Coverage**: $(if [ "$INCOMPLETE" -eq 1 ]; then echo "⚠️ INCOMPLETE — ${INCOMPLETE_REASONS}"; else echo "complete ($TOTAL entries scanned)"; fi)"
    echo "- **Duration**: $((SECONDS - START_SECONDS))s"
    echo "- **Violations found**: $((VIOLATIONS + LIT_VIOLATIONS)) (class: $VIOLATIONS, literal-gate: $LIT_VIOLATIONS)"
    echo "- **Coverage gaps (not violations)**: truncated-p1 $P1_TRUNC, truncated-p3 $P3_TRUNC, p1-error $P1_ERRORS, retention-cap $P1_RETCAP, benign read skips $P1_BENIGN, not-a-repo skips $NOT_REPO_COUNT, damaged-git entries $DAMAGED_COUNT"
    echo "- **Whole-home sweep (SCAN_HOME_SWEEP)**: $(if [ "$HOME_SWEEP_ENTRY" = "1" ]; then echo "ON — $HOME_DIR scanned last"; else echo "off (set SCAN_HOME_SWEEP=1 to also look for residue outside every repo)"; fi)"
    echo "- **Allowlist**: ${ALLOWLIST_FILE:-none}"
    echo ""
    if [ "$INCOMPLETE" -eq 1 ] && [ "$((VIOLATIONS + LIT_VIOLATIONS))" -eq 0 ]; then
        echo "⚠️ **Coverage incomplete (${INCOMPLETE_REASONS}) — absence of findings is NOT proof of absence.**"
        echo ""
    fi
    if [ "$COVERAGE_GAPS" -eq 1 ]; then
        echo "## Coverage gaps"
        echo ""
        echo "A gap is not a secret and not a clean result: it is coverage this run did"
        echo "not obtain, reported instead of being silently dropped."
        echo ""
        if [ "$P1_TRUNC" -gt 0 ] || [ "$P3_TRUNC" -gt 0 ] || [ "$P1_RETCAP" -gt 0 ]; then
            echo "### Truncated — the per-repo cap killed the scan (results are PARTIAL, never treat as clean)"
            echo ""
            echo '```'
            redact_stream "$TRUNC_LIST$P3_TRUNC_LIST"
            echo '```'
            echo ""
        fi
        if [ "$P1_ERRORS" -gt 0 ]; then
            echo "### PASS 1 grep errors (rc>=2, not a benign read skip)"
            echo ""
            echo '```'
            redact_stream "$P1_ERROR_LIST"
            echo '```'
            echo ""
        fi
        if [ "$NOT_REPO_COUNT" -gt 0 ]; then
            echo "### Not a repository (a .git git rejects AND an ancestor of other scan targets — skipped)"
            echo ""
            echo '```'
            redact_stream "$(printf '%b' "$NOT_REPO_LIST")"
            echo '```'
            echo ""
            echo "These paths were NOT scanned: their tree IS the other entries listed"
            echo "above (for \`\$HOME\` with a stray \`.git\`, that meant re-scanning the whole"
            echo "fleet, ~224s, and starving the run before any real repo).  Repair/remove"
            echo "the stray \`.git\` if the path is meant to be an entry of its own; for"
            echo "whole-home residue outside every repo use \`SCAN_HOME_SWEEP=1\`."
            echo ""
        fi
        if [ "$DAMAGED_COUNT" -gt 0 ]; then
            echo "### Damaged .git (working tree IS scanned — no committed-HEAD pass)"
            echo ""
            echo '```'
            redact_stream "$(printf '%b' "$DAMAGED_LIST")"
            echo '```'
            echo ""
            echo "Git rejects these \`.git\` directories (e.g. no HEAD), so their COMMITTED"
            echo "trees could not be checked — a literal that was committed and then"
            echo "scrubbed in the working tree is NOT visible here.  Their working trees"
            echo "were scanned normally.  Repair the repository (or delete it) to regain"
            echo "the HEAD-tree pass."
            echo ""
        fi
        if [ "$P1_BENIGN" -gt 0 ]; then
            echo "- Benign read skips (file vanished / EACCES / symlink loop) in $P1_BENIGN entr(y/ies)."
            echo ""
        fi
    fi
    echo "## Timings"
    echo ""
    echo "Slowest entries (top 5, per pass) — no single entry should dominate:"
    echo ""
    echo '```'
    sort -rn "$TIMING_FILE" | awk -F'\t' 'NR<=5 {printf "%9dms  %-6s %s\n", $1, $3, $2}'
    echo '```'
    echo ""
    if [ "$((VIOLATIONS + LIT_VIOLATIONS))" -eq 0 ]; then
        echo "✅ **No secrets detected.**$( [ "$COVERAGE_GAPS" -eq 1 ] && echo " (Coverage gaps above still apply — read them before trusting this line.)" || echo " All scanned entries are clean." )"
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
echo "Coverage gaps: truncated-p1 $P1_TRUNC / truncated-p3 $P3_TRUNC / p1-error $P1_ERRORS / retention-cap $P1_RETCAP / benign-skips $P1_BENIGN / not-a-repo $NOT_REPO_COUNT / damaged-git $DAMAGED_COUNT"
echo "Coverage: $(if [ "$INCOMPLETE" -eq 1 ]; then echo "INCOMPLETE — ${INCOMPLETE_REASONS}"; else echo complete; fi)"
echo "Report: $REPORT"

# Notify via Signal if --notify flag and anything needs attention
NOTIFY=0
for a in "$@"; do [ "$a" = "--notify" ] && NOTIFY=1; done
if [ "$NOTIFY" -eq 1 ] && { [ "$((VIOLATIONS + LIT_VIOLATIONS))" -gt 0 ] || [ "$INCOMPLETE" -eq 1 ]; }; then
    if [ "$INCOMPLETE" -eq 1 ]; then
        SUMMARY="⚠️ Secret scan INCOMPLETE (${INCOMPLETE_REASONS}) after $REPOS_SCANNED/$TOTAL entries — $((VIOLATIONS + LIT_VIOLATIONS)) violation(s) so far. See $REPORT."
    else
        SUMMARY="⚠️ Secret scan found $((VIOLATIONS + LIT_VIOLATIONS)) violations across $REPOS_SCANNED entries ($LIT_VIOLATIONS of them retired-credential literals). See $REPORT for details."
    fi
    curl -s -X POST http://localhost:8080/api/v1/rpc \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"method\":\"send\",\"params\":{\"message\":\"$SUMMARY\"},\"id\":1}" \
        2>/dev/null || true
    echo "Notification sent."
fi

rm -f "$TIMING_FILE" "$P1_ERR_FILE" 2>/dev/null || true

EXIT=0
[ "$((VIOLATIONS + LIT_VIOLATIONS))" -gt 0 ] && EXIT=1
[ "$INCOMPLETE" -eq 1 ] && EXIT=1
exit "$EXIT"
