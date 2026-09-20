#!/usr/bin/env python3
"""Wire the wallet-secret detector into the fleet pre-commit hook and the nightly sweep.
Idempotent: refuses to double-insert."""
import pathlib
import re
import sys

HOOK = pathlib.Path.home() / ".git-hooks/pre-commit"
SWEEP = pathlib.Path.home() / ".hermes/scripts/scan_all_repos_for_secrets.sh"

GATE = '''
# --------------------------------------------------------------- GATE 2.5
# Wallet material: REAL BIP-39 seed phrases (checksum-validated, all-zero test
# vector excepted) and Cashu bearer tokens. Added 2026-09-20 after a live seed
# shipped to a public branch inside a raw lab log — the configured
# detect-secrets control has no mnemonic plugin and returned 0 findings for it.
if [ $FOUND -eq 0 ] && [ -f "$SELF_DIR/detect_wallet_secrets.py" ]; then
    WS_OUT=""
    WS_RC=0
    WS_OUT="$(python3 "$SELF_DIR/detect_wallet_secrets.py" --staged 2>/dev/null)" || WS_RC=$?
    if [ "$WS_RC" -ne 0 ] || [ -n "$WS_OUT" ]; then
        echo ""
        [ -n "$WS_OUT" ] && echo "$WS_OUT"
        echo "COMMIT BLOCKED: wallet material staged (BIP-39 seed phrase or Cashu bearer token)."
        echo "  Redact the value (the surrounding line may stay) and re-stage."
        echo "  The canonical public BIP-39 test vector is allowed and only noted."
        echo "  Deliberate exception: git commit --no-verify"
        FOUND=1
    fi
fi

'''

PASS3 = '''
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
        cand=$( (cd "$repo" && run_capped "$SCAN_TIMEOUT" grep -rlIP \
            --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir=".venv" \
            --exclude-dir="__pycache__" --exclude-dir="target" --exclude-dir="dist" \
            --exclude-dir="build" --exclude="*.min.js" --exclude="*.map" \
            -E 'mnemonic|seed[ _-]?phrase|recovery phrase|cashu[AB][A-Za-z0-9_+/=-]{40,}' . 2>/dev/null) \
            | awk 'NR<=200' || true)
        [ -n "$cand" ] || continue
        ws_hits=$(printf '%s\\n' "$cand" | while IFS= read -r f; do
            python3 "$DETECT_WALLET" "$repo/$f" 2>/dev/null
        done)
        [ -n "$ws_hits" ] || continue
        ws_count=$(printf '%s\\n' "$ws_hits" | grep -c 'WALLET-SECRET' || true)
        [ "$ws_count" -gt 0 ] || continue
        VIOLATIONS=$((VIOLATIONS + ws_count))
        reponame=$(basename "$repo")
        RESULTS="${RESULTS}\\n### $reponame ($ws_count wallet-material finding(s))\\n\\`\\`\\`\\n$(printf '%s\\n' "$ws_hits" | grep 'WALLET-SECRET' | awk 'NR<=10')\\n\\`\\`\\`\\n"
    done <<< "$REPO_LIST"
fi

'''

hook_src = HOOK.read_text()
if "GATE 2.5" in hook_src:
    print("pre-commit: GATE 2.5 already present — no change")
else:
    marker = "# --------------------------------------------------------------- GATE 3"
    if marker not in hook_src:
        print("pre-commit: GATE 3 marker not found — ABORT", file=sys.stderr)
        sys.exit(2)
    HOOK.with_suffix(".bak-walletgate").write_text(hook_src)
    HOOK.write_text(hook_src.replace(marker, GATE.lstrip("\n") + marker, 1))
    print("pre-commit: GATE 2.5 inserted (backup: pre-commit.bak-walletgate)")

sweep_src = SWEEP.read_text()
if "PASS 3 — wallet material" in sweep_src:
    print("sweep: PASS 3 already present — no change")
else:
    marker = "} > \"$REPORT\""
    if marker not in sweep_src:
        print("sweep: report marker not found — ABORT", file=sys.stderr)
        sys.exit(2)
    SWEEP.with_suffix(".bak-walletpass").write_text(sweep_src)
    SWEEP.write_text(sweep_src.replace(marker, PASS3.lstrip("\n") + marker, 1))
    print("sweep: PASS 3 inserted (backup: scan_all_repos_for_secrets.sh.bak-walletpass)")
