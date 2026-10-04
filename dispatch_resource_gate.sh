#!/usr/bin/env bash
# dispatch_resource_gate.sh — Pre-dispatch resource gate with per-friend accounting.
#
# Checks RAM, CPU, and disk before allowing kanban worker dispatch.
# Tracks per-friend resource usage and enforces per-friend limits.
#
# Usage:
#   dispatch_resource_gate.sh                     # Check + dispatch (safe)
#   dispatch_resource_gate.sh --dry-run           # Check only, no dispatch
#   dispatch_resource_gate.sh --friend <name>     # Check for specific friend
#   dispatch_resource_gate.sh --json              # JSON output incl. per-friend
#                                                  # accounting (for scripting)
#   dispatch_resource_gate.sh --verbose           # Detailed human-readable output
#   dispatch_resource_gate.sh --set-friend <name> --max-workers N --max-ram MB
#                                                  # Configure friend limits
#
# Configuration: ~/.hermes/config/dispatch_resource_gate.conf
# State:         ~/.hermes/state/friend_resources.json
# Log:           ~/.hermes/logs/dispatch_resource_gate.log
#
# Exit codes:
#   0 — all checks passed (dispatch attempted if not --dry-run)
#   1 — resource check failed (dispatch blocked)
#   2 — configuration error
#   3 — per-friend limit exceeded (dispatch blocked for that friend)

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────

CONFIG_DIR="${HOME}/.hermes/config"
STATE_DIR="${HOME}/.hermes/state"
LOG_DIR="${HOME}/.hermes/logs"
CONFIG_FILE="${CONFIG_DIR}/dispatch_resource_gate.conf"
FRIEND_STATE="${STATE_DIR}/friend_resources.json"
LOG_FILE="${LOG_DIR}/dispatch_resource_gate.log"

# System-wide thresholds
DEFAULT_MIN_RAM_MB=500
DEFAULT_MAX_LOAD_RATIO=2.0        # load_avg / cpu_cores
DEFAULT_MIN_DISK_GB=2
DEFAULT_MAX_WORKERS_TOTAL=8
DEFAULT_DISPATCH_CMD="hermes kanban dispatch"

# Provider-capacity gate (2026-09-20): when NO provider is healthy in the live
# probe, no worker can run — spawning just yields a 503 + dead pid + reclaim
# churn. Block the whole tick. Fail OPEN when the probe is missing/stale (never
# wedge dispatch on a missing file). Conservative: requires zero healthy
# providers, so it never over-blocks a partially-working pool.
DEFAULT_PROVIDER_GATE=1
DEFAULT_MIN_HEALTHY_PROVIDERS=1
DEFAULT_PROVIDER_PROBE_MAX_AGE_S=1800
DEFAULT_PROVIDER_PROBE_FILE="${HOME}/.hermes/bot/provider_probe.json"

# Per-friend defaults
DEFAULT_FRIEND_MAX_WORKERS=3
DEFAULT_FRIEND_MAX_RAM_MB=2000

# ── Internal state ───────────────────────────────────────────────────────────

DRY_RUN=false
JSON_OUTPUT=false
VERBOSE=false
FRIEND_FILTER=""
FRIEND_RECORDS=""
SET_FRIEND_MODE=false
SET_FRIEND_NAME=""
SET_FRIEND_WORKERS=""
SET_FRIEND_RAM=""

MIN_RAM_MB="$DEFAULT_MIN_RAM_MB"
MAX_LOAD_RATIO="$DEFAULT_MAX_LOAD_RATIO"
MIN_DISK_GB="$DEFAULT_MIN_DISK_GB"
MAX_WORKERS_TOTAL="$DEFAULT_MAX_WORKERS_TOTAL"
DISPATCH_CMD="$DEFAULT_DISPATCH_CMD"
PROVIDER_GATE="$DEFAULT_PROVIDER_GATE"
MIN_HEALTHY_PROVIDERS="$DEFAULT_MIN_HEALTHY_PROVIDERS"
PROVIDER_PROBE_MAX_AGE_S="$DEFAULT_PROVIDER_PROBE_MAX_AGE_S"
PROVIDER_PROBE_FILE="$DEFAULT_PROVIDER_PROBE_FILE"

# ── Helpers ──────────────────────────────────────────────────────────────────

log() {
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "[$ts] $*" >> "$LOG_FILE" 2>/dev/null || true
}

die() {
    echo "ERROR: $*" >&2
    log "ERROR: $*"
    exit 2
}

# Read a config value from CONFIG_FILE if present, else echo default.
# Usage: cfg_val KEY DEFAULT
cfg_val() {
    local key="$1" default="$2"
    if [ -f "$CONFIG_FILE" ]; then
        local val
        val="$(grep -E "^${key}=" "$CONFIG_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}

load_config() {
    mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
    MIN_RAM_MB="$(cfg_val MIN_RAM_MB "$DEFAULT_MIN_RAM_MB")"
    MAX_LOAD_RATIO="$(cfg_val MAX_LOAD_RATIO "$DEFAULT_MAX_LOAD_RATIO")"
    MIN_DISK_GB="$(cfg_val MIN_DISK_GB "$DEFAULT_MIN_DISK_GB")"
    MAX_WORKERS_TOTAL="$(cfg_val MAX_WORKERS_TOTAL "$DEFAULT_MAX_WORKERS_TOTAL")"
    DISPATCH_CMD="$(cfg_val DISPATCH_CMD "$DEFAULT_DISPATCH_CMD")"
    PROVIDER_GATE="$(cfg_val PROVIDER_GATE "$DEFAULT_PROVIDER_GATE")"
    MIN_HEALTHY_PROVIDERS="$(cfg_val MIN_HEALTHY_PROVIDERS "$DEFAULT_MIN_HEALTHY_PROVIDERS")"
    PROVIDER_PROBE_MAX_AGE_S="$(cfg_val PROVIDER_PROBE_MAX_AGE_S "$DEFAULT_PROVIDER_PROBE_MAX_AGE_S")"
    PROVIDER_PROBE_FILE="$(cfg_val PROVIDER_PROBE_FILE "$DEFAULT_PROVIDER_PROBE_FILE")"
}

# ── System resource checks ───────────────────────────────────────────────────

get_available_ram_mb() {
    # Returns available RAM in MB from /proc/meminfo.
    awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0
}

get_cpu_cores() {
    local n
    n="$(nproc 2>/dev/null)" || n="$(grep -c ^processor /proc/cpuinfo 2>/dev/null)" || n=1
    echo "$n"
}

get_load1() {
    awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0
}

get_disk_free_gb() {
    # Free disk on the partition holding STATE_DIR (the hermes home).
    df -BG --output=avail "$STATE_DIR" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0
}

check_ram() {
    local avail
    avail="$(get_available_ram_mb)"
    if [ "$avail" -lt "$MIN_RAM_MB" ]; then
        echo "RAM_LOW ${avail}MB < ${MIN_RAM_MB}MB"
        return 1
    fi
    echo "${avail}"
    return 0
}

check_cpu() {
    local load cores ratio
    load="$(get_load1)"
    cores="$(get_cpu_cores)"
    ratio="$(awk -v l="$load" -v c="$cores" 'BEGIN {printf "%.2f", l/c}')"
    # Compare using awk for float safety
    if ! awk -v r="$ratio" -v m="$MAX_LOAD_RATIO" 'BEGIN {exit (r < m) ? 0 : 1}'; then
        echo "CPU_HIGH load=${load} cores=${cores} ratio=${ratio} > ${MAX_LOAD_RATIO}"
        return 1
    fi
    echo "load=${load} cores=${cores} ratio=${ratio}"
    return 0
}

check_disk() {
    local free_gb
    free_gb="$(get_disk_free_gb)"
    if [ "$free_gb" -lt "$MIN_DISK_GB" ]; then
        echo "DISK_LOW ${free_gb}GB < ${MIN_DISK_GB}GB"
        return 1
    fi
    echo "${free_gb}"
    return 0
}

# ── Provider-capacity check ──────────────────────────────────────────────────

# Count providers that are healthy AND fresh in the live probe. Block only when
# fewer than MIN_HEALTHY_PROVIDERS are usable, so a partially-working pool is
# never blocked. Fail OPEN on a missing/unreadable probe.
check_providers() {
    [ "${PROVIDER_GATE:-1}" = "1" ] || { echo "disabled"; return 0; }
    [ -f "$PROVIDER_PROBE_FILE" ] || { echo "no-probe-file"; return 0; }
    local verdict
    verdict="$(python3 - "$PROVIDER_PROBE_FILE" "$PROVIDER_PROBE_MAX_AGE_S" "$MIN_HEALTHY_PROVIDERS" 2>/dev/null <<'PY'
import json, sys, time
path, max_age, minimum = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
try:
    data = json.load(open(path))
except Exception:
    print("unreadable"); raise SystemExit(0)
if not isinstance(data, dict) or not data:
    print("unreadable"); raise SystemExit(0)
now = time.time()
healthy = 0
for rec in data.values():
    if not isinstance(rec, dict) or not rec.get("healthy"):
        continue
    try:
        if now - float(rec.get("ts", 0)) <= max_age:
            healthy += 1
    except Exception:
        pass
print(f"block {healthy}" if healthy < minimum else f"ok {healthy}")
PY
)"
    case "$verdict" in
        block\ *)
            echo "PROVIDERS_EXHAUSTED ${verdict#block } healthy < ${MIN_HEALTHY_PROVIDERS}"
            return 1
            ;;
        *)
            echo "${verdict:-unknown}"
            return 0
            ;;
    esac
}

# ── Per-friend resource accounting ───────────────────────────────────────────

# Initialize friend state file if missing.
init_friend_state() {
    if [ ! -f "$FRIEND_STATE" ]; then
        echo '{}' > "$FRIEND_STATE"
    fi
}

# Get all friend names: state-file entries PLUS any name declared only in the
# config file (FRIEND_<name>_MAX_WORKERS / FRIEND_<name>_MAX_RAM_MB).
# The config file is the documented configuration surface, so a friend whose
# limits live only there must still be accounted for by the all-friends sweep.
friend_list() {
    {
        if [ -f "$FRIEND_STATE" ]; then
            python3 -c "
import json
try:
    with open('$FRIEND_STATE') as f:
        data = json.load(f)
    for name in data.keys():
        print(name)
except Exception:
    pass
" 2>/dev/null || true
        fi
        if [ -f "$CONFIG_FILE" ]; then
            grep -oE '^FRIEND_[A-Za-z0-9_.-]+_(MAX_WORKERS|MAX_RAM_MB)=' "$CONFIG_FILE" 2>/dev/null \
                | sed -E 's/^FRIEND_(.*)_(MAX_WORKERS|MAX_RAM_MB)=$/\1/' || true
        fi
    } | sort -u
}

# Get per-friend config value (from config file or default).
# Usage: friend_cfg <name> <field> <default>
friend_cfg() {
    local name="$1" field="$2" default="$3"
    local key="FRIEND_${name}_${field}"
    cfg_val "$key" "$default"
}

# Count running workers for a specific friend by inspecting process table.
# Workers are tagged with FRIEND=<name> env var (set by dispatch wrapper).
# Falls back to counting all hermes worker processes if no friend tagging.
count_running_workers_for_friend() {
    local name="$1"
    local count
    # NB: `pgrep | wc -l` under `set -o pipefail` returns non-zero when pgrep
    # matches nothing, so a trailing `|| echo 0` appended a second line ("0\n0")
    # and broke the later `[ "$n" -ge ... ]` arithmetic. Let wc be the only
    # source of truth.
    count="$(pgrep -f 'hermes.*worker|hermes.*kanban' 2>/dev/null | wc -l)"
    # Filter by FRIEND= env var via /proc if pgrep found candidates
    if [ "$count" -gt 0 ] && [ -n "$name" ]; then
        count=0
        for pid_dir in /proc/[0-9]*/; do
            # Order matters: redirections are applied left to right, so
            # `tr ... < file 2>/dev/null` still reports the failed open on the
            # shell's stderr (one "Permission denied" line per unreadable
            # /proc/<pid>/environ — 270 lines on a 250-process host).
            # `2>/dev/null` FIRST silences the shell's own message too.
            if tr '\0' '\n' 2>/dev/null < "${pid_dir}environ" \
               | grep -q "^FRIEND=${name}$" 2>/dev/null; then
                count=$((count + 1))
            fi
        done
    fi
    echo "$count"
}

# Count total running hermes workers (all friends).
count_total_running_workers() {
    # See the pipefail note in count_running_workers_for_friend above.
    local n
    n="$(pgrep -f 'hermes.*worker|hermes.*kanban' 2>/dev/null | wc -l)"
    echo "${n:-0}"
}

# Estimate RAM usage for a friend's workers (in MB).
estimate_friend_ram_mb() {
    local name="$1"
    local total_kb=0
    # Sum RSS of processes tagged with FRIEND=<name>
    for pid_dir in /proc/[0-9]*/; do
        local env_file="${pid_dir}environ"
        if [ -r "$env_file" ]; then
            # stderr redirect BEFORE the input redirect — see the note in
            # count_running_workers_for_friend above.
            if tr '\0' '\n' 2>/dev/null < "$env_file" \
               | grep -q "^FRIEND=${name}$" 2>/dev/null; then
                local rss_kb
                rss_kb="$(awk '/^VmRSS:/ {print $2}' 2>/dev/null < "${pid_dir}status" || echo 0)"
                [ -n "$rss_kb" ] || rss_kb=0
                total_kb=$((total_kb + rss_kb))
            fi
        fi
    done
    echo $((total_kb / 1024))
}

# Collect per-friend accounting facts in a machine-readable record.
# Usage: friend_record <name>
# Emits ONE pipe-delimited line:
#   name|status|workers|max_workers|ram_mb|max_ram_mb|reasons
# status is "ok" or "blocked"; reasons is a '; '-terminated list (may be empty).
friend_record() {
    local name="$1"
    local max_workers max_ram
    max_workers="$(friend_cfg "$name" MAX_WORKERS "$DEFAULT_FRIEND_MAX_WORKERS")"
    max_ram="$(friend_cfg "$name" MAX_RAM_MB "$DEFAULT_FRIEND_MAX_RAM_MB")"

    local running ram_usage status=ok reasons=""
    running="$(count_running_workers_for_friend "$name")"
    ram_usage="$(estimate_friend_ram_mb "$name")"

    if [ "$running" -ge "$max_workers" ]; then
        status=blocked
        reasons="${reasons}WORKERS_FULL ${running}/${max_workers}; "
    fi

    if [ "$ram_usage" -gt "$max_ram" ]; then
        status=blocked
        reasons="${reasons}RAM_EXCEEDED ${ram_usage}MB > ${max_ram}MB; "
    fi

    printf '%s|%s|%s|%s|%s|%s|%s\n' \
        "$name" "$status" "$running" "$max_workers" "$ram_usage" "$max_ram" "$reasons"
}

# Field helper: friend_record_field <record> <n>  (1 = name, 2 = status, ...)
friend_record_field() {
    printf '%s' "$1" | cut -d'|' -f"$2"
}

# Render a friend_record line as the human-readable summary.
# Usage: render_friend_human "<record>"
render_friend_human() {
    local record="$1" status
    status="$(friend_record_field "$record" 2)"
    if [ "$status" = ok ]; then
        echo "workers=$(friend_record_field "$record" 3)/$(friend_record_field "$record" 4) ram=$(friend_record_field "$record" 5)/$(friend_record_field "$record" 6)MB"
    else
        friend_record_field "$record" 7
    fi
}

# Check per-friend limits.
# Usage: check_friend <name>
# Prints the human summary; returns 0 if within limits, 1 if exceeded.
check_friend() {
    local record
    record="$(friend_record "$1")"
    render_friend_human "$record"
    [ "$(friend_record_field "$record" 2)" = ok ]
}

# Set friend configuration (writes to config file).
set_friend_config() {
    mkdir -p "$CONFIG_DIR"
    touch "$CONFIG_FILE"

    # Remove existing entries for this friend's fields
    if [ -n "$SET_FRIEND_WORKERS" ]; then
        sed -i "/^FRIEND_${SET_FRIEND_NAME}_MAX_WORKERS=/d" "$CONFIG_FILE" 2>/dev/null || true
        echo "FRIEND_${SET_FRIEND_NAME}_MAX_WORKERS=${SET_FRIEND_WORKERS}" >> "$CONFIG_FILE"
    fi
    if [ -n "$SET_FRIEND_RAM" ]; then
        sed -i "/^FRIEND_${SET_FRIEND_NAME}_MAX_RAM_MB=/d" "$CONFIG_FILE" 2>/dev/null || true
        echo "FRIEND_${SET_FRIEND_NAME}_MAX_RAM_MB=${SET_FRIEND_RAM}" >> "$CONFIG_FILE"
    fi

    # Ensure friend exists in state file
    python3 -c "
import json
try:
    with open('$FRIEND_STATE') as f:
        data = json.load(f)
except Exception:
    data = {}
data.setdefault('$SET_FRIEND_NAME', {'workers': 0, 'ram_mb': 0})
with open('$FRIEND_STATE', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null

    echo "Set friend '$SET_FRIEND_NAME': max_workers=${SET_FRIEND_WORKERS:-unchanged} max_ram=${SET_FRIEND_RAM:-unchanged}MB"
    exit 0
}

# ── JSON output ──────────────────────────────────────────────────────────────

output_json() {
    local ram_avail="$1"
    local load="$2" cores="$3" cpu_ratio="$4"
    local disk_free="$5"
    local total_workers="$6"
    local system_ok="$7"
    local blocked_reasons="$8"
    local friend_records="$9"
    local friend_checked="${10}"

    # Pass values as argv to avoid shell-quoting issues in Python source.
    python3 - "$ram_avail" "$MIN_RAM_MB" "$load" "$cores" "$cpu_ratio" \
        "$MAX_LOAD_RATIO" "$disk_free" "$MIN_DISK_GB" "$total_workers" \
        "$MAX_WORKERS_TOTAL" "$system_ok" "$blocked_reasons" "$friend_checked" \
        "$DRY_RUN" "$friend_records" <<'PYEOF'
import json, sys
(
    ram_avail, min_ram, load, cores, cpu_ratio, max_ratio,
    disk_free, min_disk, total_workers, max_workers_total,
    system_ok, blocked_reasons, friend_checked, dry_run, friend_records
) = sys.argv[1:16]

# load and cpu_ratio may be bare floats or "key=value" strings.
load_val = float(load.split('=', 1)[1]) if '=' in load else float(load)
ratio_val = float(cpu_ratio.rsplit('=', 1)[1]) if '=' in cpu_ratio else float(cpu_ratio)


def split_reasons(raw):
    return [r.strip() for r in raw.strip('; ').split(';') if r.strip()]


def parse_friend_records(path):
    """Read friend_record lines: name|status|workers|max_workers|ram_mb|max_ram_mb|reasons."""
    friends = {}
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return friends
    for line in lines:
        parts = line.split('|')
        if len(parts) < 7 or not parts[0].strip():
            continue
        name, status, workers, max_workers, ram_mb, max_ram_mb, reasons = parts[:7]
        friends[name] = {
            'ok': status == 'ok',
            'workers': int(workers),
            'max_workers': int(max_workers),
            'ram_mb': int(ram_mb),
            'max_ram_mb': int(max_ram_mb),
            'reasons': split_reasons(reasons),
        }
    return friends


result = {
    'system': {
        'ram_avail_mb': int(ram_avail),
        'min_ram_mb': int(min_ram),
        'load1': load_val,
        'cpu_cores': int(cores),
        'load_ratio': ratio_val,
        'max_load_ratio': float(max_ratio),
        'disk_free_gb': int(disk_free),
        'min_disk_gb': int(min_disk),
        'total_workers': int(total_workers),
        'max_workers_total': int(max_workers_total),
        'ok': system_ok == 'true',
    },
    'blocked_reasons': split_reasons(blocked_reasons),
    'friend_checked': friend_checked,
    'friends': parse_friend_records(friend_records),
    'safe': system_ok == 'true' and not blocked_reasons.strip(),
    'dry_run': dry_run == 'true',
}
print(json.dumps(result, indent=2))
PYEOF
}

# ── Main ────────────────────────────────────────────────────────────────────

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)     DRY_RUN=true ;;
            --json)       JSON_OUTPUT=true ;;
            --verbose|-v) VERBOSE=true ;;
            --friend)     FRIEND_FILTER="$2"; shift ;;
            --set-friend) SET_FRIEND_MODE=true; SET_FRIEND_NAME="$2"; shift ;;
            --max-workers) SET_FRIEND_WORKERS="$2"; shift ;;
            --max-ram)     SET_FRIEND_RAM="$2"; shift ;;
            --help|-h)
                sed -n '2,20p' "$0"
                exit 0
                ;;
            *) die "unknown arg: $1" ;;
        esac
        shift
    done
}

main() {
    parse_args "$@"
    load_config
    init_friend_state

    # Friend config mode
    if [ "$SET_FRIEND_MODE" = true ]; then
        [ -z "$SET_FRIEND_NAME" ] && die "--set-friend requires a name"
        set_friend_config
    fi

    # ── System resource checks ──
    local ram_result cpu_result disk_result
    local blocked_reasons=""
    local system_ok=true

    ram_result="$(check_ram)" || { system_ok=false; blocked_reasons="${blocked_reasons}${ram_result}; "; }
    ram_avail="$(get_available_ram_mb)"

    cpu_result="$(check_cpu)" || { system_ok=false; blocked_reasons="${blocked_reasons}${cpu_result}; "; }
    local load cores cpu_ratio
    load="$(get_load1)"
    cores="$(get_cpu_cores)"
    cpu_ratio="$(awk -v l="$load" -v c="$cores" 'BEGIN {printf "%.2f", l/c}')"

    disk_result="$(check_disk)" || { system_ok=false; blocked_reasons="${blocked_reasons}${disk_result}; "; }
    local disk_free
    disk_free="$(get_disk_free_gb)"

    # Provider capacity: no healthy provider => no spawnable worker this tick.
    local prov_result
    prov_result="$(check_providers)" || { system_ok=false; blocked_reasons="${blocked_reasons}${prov_result}; "; }

    # ── Total worker count check ──
    local total_workers
    total_workers="$(count_total_running_workers)"
    if [ "$total_workers" -ge "$MAX_WORKERS_TOTAL" ]; then
        system_ok=false
        blocked_reasons="${blocked_reasons}AT_CAPACITY ${total_workers}/${MAX_WORKERS_TOTAL}; "
    fi

    # ── Per-friend checks ──
    local friend_checked="$FRIEND_FILTER"
    local friend_results=""
    local friends_ok=true

    # One pipe-delimited record per checked friend (see friend_record);
    # consumed by --json so scripted callers get the accounting too.
    FRIEND_RECORDS="$(mktemp "${TMPDIR:-/tmp}/dispatch_resource_gate.friends.XXXXXX")"
    # shellcheck disable=SC2064  # expand the path now — the trap fires at exit
    trap "rm -f '${FRIEND_RECORDS}'" EXIT

    local checked_friends=()
    if [ -n "$FRIEND_FILTER" ]; then
        checked_friends=("$FRIEND_FILTER")
    else
        # Check all known friends
        while IFS= read -r fname; do
            [ -n "$fname" ] && checked_friends+=("$fname")
        done < <(friend_list)
        friend_checked="(all)"
    fi

    # ${arr[@]+...} guard: an empty array under `set -u` on bash < 4.4.
    for fname in ${checked_friends[@]+"${checked_friends[@]}"}; do
        local record human fstatus
        record="$(friend_record "$fname")"
        printf '%s\n' "$record" >> "$FRIEND_RECORDS"
        human="$(render_friend_human "$record")"
        fstatus="$(friend_record_field "$record" 2)"
        if [ "$fstatus" != ok ]; then
            friends_ok=false
            blocked_reasons="${blocked_reasons}FRIEND_${fname}: ${human}; "
        fi
        if [ -n "$FRIEND_FILTER" ]; then
            friend_results="${fname}: ${human}"
        else
            friend_results="${friend_results}${fname}: ${human}\n"
        fi
    done

    local all_ok
    if [ "$system_ok" = true ] && [ "$friends_ok" = true ]; then
        all_ok=true
    else
        all_ok=false
    fi

    # Documented exit codes: 1 = system resource check failed, 3 = per-friend
    # limit exceeded. A system failure wins when both fire.
    local block_exit=1
    if [ "$system_ok" = true ] && [ "$friends_ok" != true ]; then
        block_exit=3
    fi

    # ── Output ──
    if [ "$JSON_OUTPUT" = true ]; then
        output_json "$ram_avail" "$load" "$cores" "ratio=$cpu_ratio" \
            "$disk_free" "$total_workers" "$all_ok" "$blocked_reasons" \
            "$FRIEND_RECORDS" "$friend_checked"
    elif [ "$VERBOSE" = true ]; then
        echo "=== System Resources ==="
        echo "  RAM:   ${ram_avail}MB available (min ${MIN_RAM_MB}MB)"
        echo "  CPU:   load=${load} cores=${cores} ratio=${cpu_ratio} (max ${MAX_LOAD_RATIO})"
        echo "  Disk:  ${disk_free}GB free (min ${MIN_DISK_GB}GB)"
        echo "  Workers: ${total_workers}/${MAX_WORKERS_TOTAL} running"
        echo "=== Per-Friend ==="
        if [ -n "$FRIEND_FILTER" ]; then
            echo "  $friend_results"
        else
            echo -e "  $friend_results"
        fi
        echo "=== Verdict ==="
        if [ "$all_ok" = true ]; then
            echo "  PASS — dispatch allowed"
        else
            echo "  BLOCKED — ${blocked_reasons}"
        fi
    else
        # Compact output
        if [ "$all_ok" = true ]; then
            echo "OK ram=${ram_avail}MB cpu=${cpu_ratio} disk=${disk_free}GB workers=${total_workers}"
        else
            echo "BLOCKED ${blocked_reasons}"
        fi
    fi

    # ── Dispatch or log ──
    if [ "$all_ok" = true ] && [ "$DRY_RUN" = false ]; then
        log "PASS — dispatching"
        echo "Dispatching..." >&2
        # shellcheck disable=SC2086
        if ! $DISPATCH_CMD 2>&1; then
            log "dispatch command failed"
            exit 1
        fi
        log "dispatch complete"
    elif [ "$all_ok" = true ] && [ "$DRY_RUN" = true ]; then
        echo "(dry-run — would dispatch)" >&2
    else
        log "BLOCKED: ${blocked_reasons}"
        exit "$block_exit"
    fi

    exit 0
}

main "$@"