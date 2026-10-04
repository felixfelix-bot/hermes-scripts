#!/usr/bin/env bash
# router_bench_test.sh — gateway-side dispatcher for router tests.
#
# Runs on the ELECTED `router-bench-gateway` (fleet host reachable from the
# ngit-ci coordinator and from the test targets). Invoked by the backend repo's
# router-test workflow after it SSHes to the gateway.
#
# Responsibilities:
#   * gate PR runs on BOTH the Kalman resource gate and the lab gate;
#     `main` runs regardless (still serialized by the workflow concurrency).
#   * select targets: PR -> QEMU lab only; main -> lab + physical bench.
#   * run the framework suite and report which target was used.
#
# Usage:
#   router_bench_test.sh --event push|pr --ref REF --commit SHA \
#       [--target auto|lab|physical|both] [--lane readonly|mutating] \
#       [--paid false|true] [--results-dir DIR]
set -euo pipefail

EVENT=push; REF=main; COMMIT=""; TARGET=auto; LANE=readonly; PAID=false
RESULTS="${HOME}/reports/router-test"
while [ $# -gt 0 ]; do
  case "$1" in
    --event) EVENT="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --lane) LANE="$2"; shift 2 ;;
    --paid) PAID="$2"; shift 2 ;;
    --results-dir) RESULTS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$RESULTS"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$RESULTS/$TS"
mkdir -p "$OUT"
log() { echo "[router-bench] $*" | tee -a "$OUT/log"; }

FW="${ROUTER_BENCH_FRAMEWORK_DIR:-$HOME/src/physical-router-test-automation}"
LAB_HOST="${ROUTER_BENCH_LAB_HOST:-c08r4d0r@192.168.2.18}"
BENCH_IP="${ROUTER_BENCH_PHYSICAL_IP:-192.168.1.1}"
LAB_GATE_LOCAL="${ROUTER_BENCH_LAB_GATE:-$HOME/.hermes/scripts/openwrt_lab_gate.py}"
RES_GATE="${ROUTER_BENCH_RESOURCE_GATE:-$HOME/.hermes/scripts/resource_gate.py}"

log "event=$EVENT ref=$REF commit=${COMMIT:-?} target=$TARGET lane=$LANE paid=$PAID"

# ── gates ───────────────────────────────────────────────────────────────────
gate_resource() { [ -x "$RES_GATE" ] || [ -f "$RES_GATE" ] || return 0; python3 "$RES_GATE" >/dev/null 2>&1; }
gate_lab() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$LAB_HOST" \
      "python3 ~/.hermes/scripts/openwrt_lab_gate.py" >/dev/null 2>&1
}

if [ "$EVENT" = pr ]; then
  if gate_resource && gate_lab; then
    log "gates: resource=green lab=green -> PR run allowed"
  else
    log "gates: resource=$(gate_resource && echo green || echo red) lab=$(gate_lab && echo green || echo red) -> PR run SKIPPED (main-only under low resources)"
    echo '{"run":false,"reason":"gates-red-under-low-resources"}' > "$OUT/result.json"
    exit 0
  fi
fi

# ── target selection ────────────────────────────────────────────────────────
reachable() { timeout 5 bash -c "echo > /dev/tcp/$1/22" 2>/dev/null; }
targets=()
case "$TARGET" in
  lab) targets=(lab) ;;
  physical) targets=(physical) ;;
  both) targets=(lab physical) ;;
  auto)
    if [ "$EVENT" = pr ]; then targets=(lab)
    else targets=(lab); reachable "$BENCH_IP" && targets+=(physical); fi ;;
esac
log "targets selected: ${targets[*]}"

# ── run ─────────────────────────────────────────────────────────────────────
RC=0
USED=()
for t in "${targets[@]}"; do
  if [ "$t" = lab ]; then
    log "running lab suite on $LAB_HOST"
    if ssh -o BatchMode=yes -o ConnectTimeout=10 "$LAB_HOST" \
        "cd ~/src/physical-router-test-automation && BACKEND=${BACKEND:-go} TIER=${TIER:-smoke} ./scripts/router-vm-smoke.sh" \
        >>"$OUT/lab.log" 2>&1; then
      USED+=("lab:x280"); else RC=1; USED+=("lab:x280(FAIL)"); fi
  fi
  if [ "$t" = physical ]; then
    log "running physical suite on bench $BENCH_IP"
    if [ "$PAID" = true ]; then PAIDFLAG=(); else PAIDFLAG=(--no-paid); fi
    if (cd "$FW" && ./scripts/test-pr.sh --branch "$REF" --router tollgate-7TJZ \
          --backend "${BACKEND:-go}" "${PAIDFLAG[@]}") >>"$OUT/physical.log" 2>&1; then
      USED+=("physical:tollgate-7TJZ"); else RC=1; USED+=("physical:tollgate-7TJZ(FAIL)"); fi
  fi
done

cat > "$OUT/result.json" <<EOF
{"run":true,"event":"$EVENT","ref":"$REF","commit":"$COMMIT",
 "lane":"$LANE","paid":$PAID,"targets_used":"${USED[*]:-none}","rc":$RC}
EOF
log "done rc=$RC targets_used=${USED[*]:-none} results=$OUT"
exit "$RC"
