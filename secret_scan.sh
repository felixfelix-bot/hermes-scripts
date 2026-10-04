#!/usr/bin/env bash
# secret_scan.sh — single-pass secret scanner (gitleaks if present, else regex).
#
# Used by: .githooks/pre-commit, .githooks/pre-push, GitHub Actions secret-scan
# job, and the gate_engine `secrets_clean` evidence. One pass over the target
# text (fast), with allowlist + inline `# EPHEMERAL-KEY` / `# SECRET-OK` skips.
#
# Usage:
#   secret_scan.sh --staged            # scan the staged diff (pre-commit)
#   secret_scan.sh --range A..B        # scan a commit range (pre-push / CI)
#   secret_scan.sh --path DIR          # scan a working tree
# Exit: 0 clean, 1 findings, 2 usage error.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PATTERNS_FILE="$ROOT/.secret-patterns.txt"
ALLOWLIST_FILE="${SECRET_SCAN_ALLOWLIST:-$HOME/.secret-scan-allowlist.txt}"

MODE="staged"; ARG=""
case "${1:-}" in
  --staged) MODE="staged" ;;
  --range)  MODE="range"; ARG="${2:-}" ;;
  --path)   MODE="path";  ARG="${2:-}" ;;
  *) echo "usage: secret_scan.sh [--staged|--range A..B|--path DIR]" >&2; exit 2 ;;
esac

# gitleaks path (preferred) ----------------------------------------------------
if command -v gitleaks >/dev/null 2>&1; then
  # Only pass -c when the config exists; a missing -c path makes gitleaks exit
  # non-zero (fail-closed), which would block commits in config-less repos.
  GL_CFG=()
  [ -f "$ROOT/.gitleaks.toml" ] && GL_CFG=(-c "$ROOT/.gitleaks.toml")
  case "$MODE" in
    staged) gitleaks protect --staged --redact --no-banner "${GL_CFG[@]}" >/dev/null 2>&1; rc=$? ;;
    range)  gitleaks detect --log-opts="$ARG" --redact --no-banner "${GL_CFG[@]}" >/dev/null 2>&1; rc=$? ;;
    path)   gitleaks detect --no-git --source "$ARG" --redact --no-banner "${GL_CFG[@]}" >/dev/null 2>&1; rc=$? ;;
  esac
  rc="${rc:-1}"
  [ "$rc" -eq 0 ] && { echo "secret-scan: clean (gitleaks $(gitleaks version 2>/dev/null | head -1))"; exit 0; }
  echo "secret-scan: FINDINGS (gitleaks, rc=$rc)" >&2
  exit 1
fi

# regex fallback ---------------------------------------------------------------
[ -f "$PATTERNS_FILE" ] || { echo "secret-scan: no patterns file"; exit 0; }
COMBINED="$(grep -vE '^\s*#|^\s*$' "$PATTERNS_FILE" | paste -sd'|' -)"
ALLOW=""
[ -f "$ALLOWLIST_FILE" ] && ALLOW="$(grep -vE '^\s*#|^\s*$' "$ALLOWLIST_FILE" | paste -sd'|' -)"

case "$MODE" in
  staged) TEXT="$(git diff --cached -U0 2>/dev/null)" ;;
  range)  TEXT="$(git log -p "$ARG" 2>/dev/null)" ;;
  path)   TEXT="$(grep -rInI . "$ARG" 2>/dev/null)" ;;
esac

HITS="$(printf '%s\n' "$TEXT" \
  | grep -nE "$COMBINED" 2>/dev/null \
  | grep -vE 'EPHEMERAL-KEY|SECRET-OK' 2>/dev/null)"
[ -n "$ALLOW" ] && HITS="$(printf '%s\n' "$HITS" | grep -vE "$ALLOW" 2>/dev/null)"
HITS="$(printf '%s\n' "$HITS" | grep '^[0-9]' 2>/dev/null | head -20)"

if [ -n "$HITS" ]; then
  echo "secret-scan: FINDINGS (regex fallback):" >&2
  printf '%s\n' "$HITS" | sed -E 's/(nsec1|ghp_|sk-)[A-Za-z0-9_-]{8,}/\1***REDACTED***/g' >&2
  exit 1
fi
echo "secret-scan: clean (regex fallback)"
exit 0
