#!/usr/bin/env bash
# Router darkness watchdog — activity-gated, emits ONLY on state transitions.
# Checks: ping LAN IP + Ethernet carrier + net4sats SSID presence.
STATE=/tmp/router-watchdog.state
HOST=192.168.1.1
ts() { date '+%H:%M:%S'; }

dark=1
ping -c1 -W2 "$HOST" >/dev/null 2>&1 && dark=0
# Secondary signals for the alert text (don't change verdict)
carrier=$(cat /sys/class/net/enp0s31f6/carrier 2>/dev/null || echo "?")
ssid=$(nmcli -t -f SSID device wifi list 2>/dev/null | grep -c '^net4sats$' || true)

prev=$(cat "$STATE" 2>/dev/null || echo "0")
echo "$dark" > "$STATE"

if [ "$prev" = "0" ] && [ "$dark" = "1" ]; then
  echo "ROUTER DARK at $(ts): $HOST stopped answering. eth carrier=$carrier, net4sats SSID count=$ssid. Check: power LED, USB-C adapter seating, wall outlet, Ethernet cable both ends. Deploy of portal-fix is blocked until it returns."
elif [ "$prev" = "1" ] && [ "$dark" = "0" ]; then
  echo "ROUTER BACK at $(ts): $HOST answering ping again (carrier=$carrier, net4sats=$ssid). Ready for deploy + 12 pre-checks."
fi
# else: no change — stay silent
