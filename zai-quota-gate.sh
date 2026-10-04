#!/usr/bin/env bash
# zai-quota-gate.sh — DEPRECATED compatibility shim (Phase R).
#
# The gate was renamed to `dispatch-gate.sh`: it is no longer zai-specific (it
# reads the proxy's /v1/dispatch_gate, which covers every lane). Existing
# callers (manager scripts, docs, crons) that still invoke `zai-quota-gate.sh`
# keep working through this shim. Update call sites to `dispatch-gate.sh`; this
# file will be removed once the migration completes.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for cand in "$HERE/dispatch-gate.sh" "$HOME/.hermes/scripts/dispatch-gate.sh"; do
  if [ -x "$cand" ]; then exec "$cand" "$@"; fi
done
echo "zai-quota-gate.sh: dispatch-gate.sh not found (Phase R rename)" >&2
exit 1
