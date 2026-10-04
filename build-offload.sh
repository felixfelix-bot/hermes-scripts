#!/usr/bin/env bash
# Canonical source: scripts/fleet/build-offload.sh (repo) — installed to
# ~/.hermes/scripts/build-offload.sh by role 79-build-runner. Edit HERE, then
# converge; the RUNNERS list below must match the [build_runners] inventory
# group (tests/test_build_runner_role.py enforces it).
# build-offload.sh — run a heavy build/test tree on a dedicated build runner
# (hermes-nvme primary; x240 overflow) instead of on the Hermes host, so a
# `go build -race` / `make test` never competes with the gateway or workers.
#
# Usage:
#   build-offload.sh [--sync-back] <src-dir> <command...>
#
# Examples:
#   build-offload.sh ~/worktrees/t_abc go test -race ./...
#   build-offload.sh --sync-back ~/worktrees/t_abc make dist
#
# Env:
#   BUILD_RUNNER_FORCE=hermes-nvme|x240   pin a runner (skip the load probe)
#
# Falls back to running locally (nice/ionice) when no runner is free.
set -uo pipefail
export LC_ALL=C LANG=C

SYNC_BACK=0
if [ "${1:-}" = "--sync-back" ]; then SYNC_BACK=1; shift; fi
SRC="${1:?usage: build-offload.sh [--sync-back] <src-dir> <command...>}"
shift
[ "$#" -gt 0 ] || set -- make
SRC="$(cd "$SRC" && pwd)"
LABEL="$(basename "$SRC")-$(date +%Y%m%d%H%M%S)-$$"
REMOTE_BASE="/srv/hermes-build"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)

# name|host|key
RUNNERS=(
  "hermes-nvme|debian@23.182.128.219|/home/c03rad0r/.ssh/id_hermes_vps"
  "x240|c08r4d0r@192.168.2.25|/home/c03rad0r/.ssh/id_ed25519"
)

pick_runner() {
  local spec name host key
  for spec in "${RUNNERS[@]}"; do
    IFS='|' read -r name host key <<<"$spec"
    [ -r "$key" ] || continue
    if [ -n "${BUILD_RUNNER_FORCE:-}" ]; then
      [ "$name" = "$BUILD_RUNNER_FORCE" ] || continue
      if ssh "${SSH_OPTS[@]}" -i "$key" "$host" true >/dev/null 2>&1; then
        printf '%s|%s|%s\n' "$name" "$host" "$key"
        return 0
      fi
      continue
    fi
    if ssh "${SSH_OPTS[@]}" -i "$key" "$host" \
        'python3 -c "import os,shutil,sys;l=os.getloadavg()[0];c=os.cpu_count() or 1;f=shutil.disk_usage(\"/\").free;sys.exit(0 if (l/c<1.0 and f>15*1024**3) else 1)"' \
        >/dev/null 2>&1; then
      printf '%s|%s|%s\n' "$name" "$host" "$key"
      return 0
    fi
  done
  return 1
}

if ! SEL="$(pick_runner)"; then
  echo "build-offload: no free runner; running locally under nice/ionice" >&2
  # The runner path always cds into the staged tree (hermes-build-run `cd $d`);
  # the local fallback must be a real substitute: run IN the source tree, not in
  # the caller's cwd. Found live 2026-10-01: `go vet ./...` ran in $HOME instead.
  cd "$SRC" || exit 3
  exec nice -n 15 ionice -c3 "$@"
fi
IFS='|' read -r NAME HOST KEY <<<"$SEL"
RDIR="$REMOTE_BASE/$LABEL"
echo "build-offload: runner=$NAME host=$HOST dir=$RDIR cmd=$*" >&2

ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" "mkdir -p '$RDIR'" || exit 3

RSYNC_EXCLUDES=(--exclude '.venv' --exclude 'node_modules' --exclude 'target'
                --exclude 'dist' --exclude 'build' --exclude '.gocache')
if [ "${BUILD_OFFLOAD_INCLUDE_BUILT:-0}" = "1" ]; then
  # Keep node_modules / dist in the transfer (e.g. a tree whose install or build
  # is more expensive than the rsync).
  RSYNC_EXCLUDES=(--exclude '.venv' --exclude 'target' --exclude '.gocache')
fi
if ! rsync -aHAX --delete --partial \
      "${RSYNC_EXCLUDES[@]}" \
      -e "ssh ${SSH_OPTS[*]} -i $KEY" "$SRC/" "$HOST:$RDIR/"; then
  echo "build-offload: rsync to runner failed; running locally" >&2
  ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" "rm -rf '$RDIR'" >/dev/null 2>&1 || true
  # Same substitute-for-the-runner contract as the pick_runner fallback above.
  cd "$SRC" || exit 3
  exec nice -n 15 ionice -c3 "$@"
fi

REMOTE_PATH="/usr/local/go/bin:\$HOME/.bun/bin:/usr/local/bin:\$PATH"

# TS/Node trees arrive without node_modules (rsync excludes it), so install once on
# the runner before the real command. Skip with BUILD_OFFLOAD_SKIP_INSTALL=1.
if [ "${BUILD_OFFLOAD_SKIP_INSTALL:-0}" != "1" ] && \
   ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" "test -f '$RDIR/package.json'" 2>/dev/null; then
  echo "build-offload: bun install on runner" >&2
  ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" \
    "PATH=$REMOTE_PATH hermes-build-run '$RDIR' bun install --frozen-lockfile" \
    || ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" \
       "PATH=$REMOTE_PATH hermes-build-run '$RDIR' bun install"

  # `dist/` is excluded from the rsync, so workspace packages that import each
  # other through their built entry points are absent until the tree is built.
  # Without this the suite fails at COLLECTION time (missing @cashu/coco-*),
  # which reads as red but is a harness artifact. Run the repo's own
  # workspace-build script when it has one.
  if ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" \
      "python3 -c \"import json,sys;print('yes' if '${BUILD_OFFLOAD_SETUP_SCRIPT:-build:test:core}' in json.load(open('$RDIR/package.json')).get('scripts',{}) else 'no')\"" 2>/dev/null | grep -q yes; then
    echo "build-offload: workspace build on runner (${BUILD_OFFLOAD_SETUP_SCRIPT:-build:test:core})" >&2
    ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" \
      "PATH=$REMOTE_PATH hermes-build-run '$RDIR' bun run ${BUILD_OFFLOAD_SETUP_SCRIPT:-build:test:core}" >&2
  fi
fi

CMD_Q="$(printf '%q ' "$@")"
set +e
ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" \
  "PATH=$REMOTE_PATH hermes-build-run '$RDIR' $CMD_Q"
rc=$?
set -e

if [ "$SYNC_BACK" = 1 ]; then
  rsync -aHAX --exclude '.venv' --exclude 'node_modules' \
    -e "ssh ${SSH_OPTS[*]} -i $KEY" "$HOST:$RDIR/" "$SRC/" || true
fi
ssh "${SSH_OPTS[@]}" -i "$KEY" "$HOST" "rm -rf '$RDIR'" >/dev/null 2>&1 || true
exit "$rc"
