# Wallet-material detector (canonical copy)

Live location: `~/.git-hooks/detect_wallet_secrets.py` (invoked by `~/.git-hooks/pre-commit` GATE 2.5).
This directory is the versioned copy. `wire-wallet-detector.py` re-installs the pre-commit gate and
the nightly-sweep PASS 3 idempotently.

Detects: real BIP-39 seed phrases (checksum-validated; the canonical all-zero test vector is exempt)
and Cashu bearer tokens. Origin: a live seed + bearer tokens reached a public branch on 2026-09-20
inside raw lab logs; the configured detect-secrets control cannot see a seed.
