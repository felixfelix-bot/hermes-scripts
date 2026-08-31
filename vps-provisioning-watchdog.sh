#!/usr/bin/env bash
# VPS provisioning watchdog — silent when both boxes dark, reports when one comes up.
# no_agent cron: empty stdout = silent, non-empty = delivered to user.
set -euo pipefail

STATE_DIR="$HOME/.hermes/state/vps-watchdog"
mkdir -p "$STATE_DIR"

check_box() {
    local name="$1" ip="$2" pass="$3"
    local state_file="$STATE_DIR/${name}.up"

    # Quick ping first (cheaper than SSH timeout)
    if ! ping -c1 -W3 "$ip" >/dev/null 2>&1; then
        # Ping failed — try SSH in case ICMP is blocked
        if ! sshpass -p "$pass" ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
             -o BatchMode=no -o LogLevel=ERROR "root@$ip" 'echo OK' >/dev/null 2>&1; then
            # Still down — clear state if it was up before
            rm -f "$state_file"
            return 1
        fi
    fi

    # Box is up — check if this is a NEW transition (was down before)
    if [ ! -f "$state_file" ]; then
        touch "$state_file"
        # Full probe on first-up detection
        local info
        info=$(sshpass -p "$pass" ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
              -o LogLevel=ERROR "root@$ip" \
              'hostname; grep PRETTY /etc/os-release 2>/dev/null; nproc; free -h | awk "NR==2{print \$2}"; df -h / | awk "NR==2{print \$2,\$4}"; which docker >/dev/null 2>&1 && docker --version || echo NO_DOCKER' 2>/dev/null || echo "(probe failed)")
        echo "VPS UP: ${name} @ ${ip}"
        echo "---"
        echo "$info"
        echo "---"
        echo "SSH: sshpass -p '<redacted>' ssh root@${ip}"
        echo "Password stored in watchdog config. Box was provisioned successfully."
    fi
    return 0
}

# Both boxes still down?
down_count=0
check_box "hermes" "23.182.128.65" "wasp-skill-fatal-armor-paper-spike" || down_count=$((down_count+1))
check_box "hermes2" "64.188.7.237" "drift-oil-kick-someone-leopard-brick" || down_count=$((down_count+1))

# If both still down — emit nothing (silent watchdog pattern)
if [ "$down_count" -eq 2 ]; then
    exit 0
fi

# If any box came up, the check_box function already printed its alert.
# Exit 0 — we don't want error alerts, just the stdout message.
exit 0
