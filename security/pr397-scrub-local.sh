#!/usr/bin/env bash
# Scrub the leaked BIP-39 seed + Cashu bearer tokens out of the #397 branch history
# and force-push the redacted branch to the felixfelix-bot fork.
#
# Rules are REGEX-based so the secret phrase is never written to disk in a rules file.
set -euo pipefail

SRC="$HOME/repos/tollgate-module-basic-go"
WORK="$HOME/repos/_scrub397"
BRANCH="research/wallet-migration"
RULES="$HOME/reports/security/filter-rules.txt"

cat > "$RULES" <<'EOF'
# literal rules would embed the secret; regex keeps it out of this file
regex:cdk-walletd: generated mnemonic:[ \t]*[a-z]+(?:[ \t]+[a-z]+){11,23}==>cdk-walletd: generated mnemonic: [REDACTED-BIP39-SEED]
regex:cashu[AB][A-Za-z0-9_+/=-]{40,}==>cashu[REDACTED-BEARER-TOKEN]
EOF
chmod 600 "$RULES"

rm -rf "$WORK"
git clone --quiet --no-hardlinks --branch "$BRANCH" "$SRC" "$WORK"

echo "--- before: occurrences in history"
cd "$WORK"
echo "seed-context lines: $(git rev-list --all | xargs -I{} git grep -c 'cdk-walletd: generated mnemonic:' {} 2>/dev/null | wc -l)"
echo "token lines       : $(git rev-list --all | xargs -I{} git grep -c 'cashuB' {} 2>/dev/null | wc -l)"

git filter-repo --replace-text "$RULES" --force --quiet

echo "--- after: occurrences in history"
echo "seed-context lines: $(git rev-list --all | xargs -I{} git grep -c 'cdk-walletd: generated mnemonic:' {} 2>/dev/null | wc -l)"
echo "raw token lines   : $(git rev-list --all | xargs -I{} git grep -c 'cashuB[0-9A-Za-z_+/=-]\{40,\}' {} 2>/dev/null | wc -l)"
echo "redaction markers : $(git rev-list --all | xargs -I{} git grep -c 'REDACTED-BIP39-SEED\|REDACTED-BEARER-TOKEN' {} 2>/dev/null | wc -l)"
echo "new head: $(git rev-parse HEAD)"
echo "commit count: $(git rev-list --count HEAD)"
echo
echo "NEXT: run  bash ~/reports/security/pr397-scrub-push.sh   to publish it"
