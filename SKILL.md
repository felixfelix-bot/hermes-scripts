---
name: memory-trim
title: Safe Memory Trimming
description: Identifies and kills non-essential swap hogs, surfaces doubtful ones for user review.
trigger: high system swap, OOM risk, dispatch-daemon stuck in WARN
---

# Memory Trimming Skill

## When to Use

- Swap > 6GB and daemon stuck in WARN state
- Before dispatching resource-intensive workers
- OOM risk (swap approaching physical RAM)

## How to Run

```bash
# Dry-run first (safe to run anytime):
python3 ~/.hermes/scripts/memory-trim.py --threshold 300 --dry-run

# Actually kill SAFE processes:
python3 ~/.hermes/scripts/memory-trim.py --threshold 300
```

## Categorization

| Category | Action | Examples |
|----------|--------|---------|
| SAFE | Kills automatically | LSP servers, vitest/jest, playwright, node, npm |
| DOUBTFUL | Surfaces for user review | hermes gateway (if 3+ copies), python3, java |
| UNSAFE | Never kills | browsers, terminals, SSH, VPN, code editors |

## Flags

- `--threshold <MB>` — minimum swap to consider (default: 300)
- `--dry-run` — no-op, just report
- `--json` — machine-readable output

## Pitfalls

- Gateway processes (hermes) are DOUBTFUL — old gateway restarts accumulate. Kill stale copies with user approval.
- signal-cli (java) can take 600MB — DOUBTFUL because it's a daemon. Safe to kill if signal bridge is down.
- DO NOT kill netbird, wireguard, or gnome-shell — they're UNSAFE.
