#!/usr/bin/env bash
# nostr-healthcheck.sh — Hermes cron watchdog for the Nostr Tier-1 surface.
#
# Probes the Tier-1 Nostr services (setup-nostr-core.yml) over HTTP. For any
# service returning >=500 or unreachable, it attempts an idempotent auto-fix by
# re-running the matching ansible-playbook with the CORRECT inventory host
# (vps1/vps2 — not the broken vps-1/vps-2 labels in the legacy watchdog.json).
# Each service is re-probed after its fix; a per-service 30-min cooldown
# prevents hammering a genuinely broken service.
#
# Output contract (Hermes no_agent cron): non-empty stdout is delivered verbatim
# to the operator; EMPTY stdout = SILENT (everything healthy, nothing to report).
# Exit 0 always (non-zero exit would raise a spurious error alert).

set -u
umask 077

REPO="/home/c03rad0r/tollgate-infrastructure-kit"
ENV_FILE="$REPO/.env"
ANSIBLE_DIR="$REPO/ansible"
STATE_DIR="${HOME}/.local/state/nostr-healthcheck"
LOG_FILE="${HOME}/.local/log/nostr-healthcheck.log"
LOCK_FILE="${HOME}/.local/state/nostr-healthcheck/.lock"
COOLDOWN_MIN=30          # min minutes between auto-fix attempts per service
HTTP_TIMEOUT=8           # seconds per single HTTP probe attempt
PROBE_RETRIES=3          # consecutive failures required before declaring DOWN
PROBE_RETRY_DELAY=8      # seconds between probe attempts (confirmation window)
ANSIBLE_TIMEOUT=600      # seconds per playbook run
# NH_CHECK_ONLY=1  -> probe + report only, never run ansible (monitor mode)
# NH_DRY_RUN=1     -> like monitor, but also prints the ansible command each
#                     DOWN service WOULD trigger (no execution). Preview mode.

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
# log <fmt> [args...] — printf-style; timestamp prefix is prepended.
log() { local fmt="$1"; shift; printf '[%s] '"$fmt"'\n' "$(ts)" "$@" >> "$LOG_FILE"; }

# ---- load .env (robust: skip blank/comment, split on first '=', strip quotes) ----
load_env() {
  [ -r "$ENV_FILE" ] || { log "FATAL: .env not readable: $ENV_FILE"; return 1; }
  local key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    [ -z "${line//[[:space:]]/}" ] && continue
    key="${line%%=*}"
    val="${line#*=}"
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    export "$key=$val"
  done < "$ENV_FILE"
  return 0
}

# ---- service table: name|url|playbook|limit_host|extra_evars ----
# Tier-1 Nostr core (setup-nostr-core.yml) + agg relay (missed by legacy watchdog).
SERVICES=(
  "caddy|https://orangesync.tech|04-caddy.yml|vps1|"
  "relay1|https://relay1.orangesync.tech|05-strfry.yml|vps1|"
  "agg|https://agg.orangesync.tech|37-strfry-agg.yml|vps2|target=vps2"
  "obelisk|https://chat.orangesync.tech|06-obelisk-relay.yml|vps1|"
  "blossom1|https://blossom1.orangesync.tech|07-blossom.yml|vps1|"
  "nsite-gateway|https://nsite.orangesync.tech|08-nsite-gateway.yml|vps1|"
  "ngit1|https://ngit1.orangesync.tech|19-ngit-relay.yml|vps1|"
)

# returns 0 if URL healthy (at least one attempt returns http code < 500).
# Retries PROBE_RETRIES times so transient 000/connection blips (Cloudflare,
# network jitter) don't cause false DOWN verdicts and spurious ansible churn.
probe() {
  local url="$1" code attempt
  for attempt in $(seq 1 "$PROBE_RETRIES"); do
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$HTTP_TIMEOUT" \
           -A 'nostr-healthcheck/1.0' "$url" 2>/dev/null)
    if [ -n "$code" ] && [ "$code" != "000" ] && [ "$code" -lt 500 ] 2>/dev/null; then
      return 0
    fi
    [ "$attempt" -lt "$PROBE_RETRIES" ] && sleep "$PROBE_RETRY_DELAY"
  done
  return 1
}

# returns 0 if a redeploy for $1 is allowed (absent or past cooldown), 1 if throttled
cooldown_ok() {
  local name="$1" marker="$STATE_DIR/${name}.redeploy"
  [ -f "$marker" ] || return 0
  find "$marker" -mmin -"$COOLDOWN_MIN" >/dev/null 2>&1 && return 1
  return 0
}
mark_redeploy() { : > "$STATE_DIR/${1}.redeploy"; }

auto_fix() {
  local playbook="$1" host="$2" evars="${3:-}" args=() ev
  [ -n "$evars" ] && while IFS= read -r ev; do
    [ -n "$ev" ] && args+=(-e "$ev")
  done <<< "$evars"
  log "auto-fix: ansible-playbook $playbook --limit $host ${args[*]:-}"
  ( cd "$ANSIBLE_DIR" && timeout "$ANSIBLE_TIMEOUT" \
    ansible-playbook "playbooks/$playbook" -i inventory/hosts.yml \
      --limit "$host" "${args[@]}" ) >>"$LOG_FILE" 2>&1
}

main() {
  # flock: skip if a previous run is still mid-ansible (prevents overlapping
  # redeploys of the same service on the same host).
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "skip: another instance holds the lock"
    return 0
  fi

  load_env || return 0   # silent on env failure (alert would be noisy/no creds)

  log "run start"

  local check_only="${NH_CHECK_ONLY:-0}"
  local dry_run="${NH_DRY_RUN:-0}"
  declare -a down_fixed=() down_still=() down_throttled=() down_nofix=() down_preview=()
  local total=0 healthy=0

  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name url playbook host evars <<< "$svc"
    total=$((total+1))
    if probe "$url"; then
      healthy=$((healthy+1))
      continue
    fi
    log "DOWN: $name ($url)"
    if [ "$check_only" = "1" ]; then
      down_nofix+=("$name")
    elif [ "$dry_run" = "1" ]; then
      local evstr="${evars:+ -e $evars}"
      down_preview+=("$name → ansible-playbook $playbook --limit $host$evstr")
    elif cooldown_ok "$name"; then
      auto_fix "$playbook" "$host" "$evars"   # rc ignored — verdict is the re-probe
      mark_redeploy "$name"
      sleep 3
      if probe "$url"; then down_fixed+=("$name"); else down_still+=("$name"); fi
    else
      down_throttled+=("$name")
    fi
  done

  # Silent when fully healthy and nothing needed action.
  if [ "$healthy" -eq "$total" ] \
     && [ ${#down_fixed[@]} -eq 0 ] && [ ${#down_still[@]} -eq 0 ] \
     && [ ${#down_throttled[@]} -eq 0 ] && [ ${#down_nofix[@]} -eq 0 ] \
     && [ ${#down_preview[@]} -eq 0 ]; then
    log "run done: all %d healthy (silent)" "$total"
    return 0
  fi

  # Build report (delivered to operator via cron stdout).
  {
    local mode=""
    [ "$check_only" = "1" ] && mode=" [monitor-only]"
    [ "$dry_run" = "1" ] && mode=" [dry-run]"
    printf '🛰️ Nostr health-check %s%s\n\n' "$(ts)" "$mode"
    [ "$healthy" -gt 0 ] && printf '✅ UP: %d/%d services healthy\n' "$healthy" "$total"
    [ ${#down_fixed[@]} -gt 0 ]     && printf '\n🔧 AUTO-FIXED (recovered after ansible redeploy): %s\n' "${down_fixed[*]}"
    [ ${#down_still[@]} -gt 0 ]     && printf '\n🔴 STILL DOWN after auto-fix: %s\n' "${down_still[*]}"
    [ ${#down_throttled[@]} -gt 0 ] && printf '\n⏸️  DOWN — auto-fix throttled (%dm cooldown): %s\n' "$COOLDOWN_MIN" "${down_throttled[*]}"
    [ ${#down_nofix[@]} -gt 0 ]     && printf '\n🔴 DOWN (monitor-only, no auto-fix): %s\n' "${down_nofix[*]}"
    if [ ${#down_preview[@]} -gt 0 ]; then
      printf '\n🔍 DOWN — would auto-fix (dry-run, not executed):\n'
      printf '   • %s\n' "${down_preview[@]}"
    fi
    printf '\nlog: %s\n' "$LOG_FILE"
  }
  log "run done: healthy=%d/%d fixed=%d still=%d throttled=%d nofix=%d preview=%d" \
    "$healthy" "$total" "${#down_fixed[@]}" "${#down_still[@]}" \
    "${#down_throttled[@]}" "${#down_nofix[@]}" "${#down_preview[@]}"
  return 0
}

main "$@"
