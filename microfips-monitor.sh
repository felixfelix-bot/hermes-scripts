#!/bin/bash

# MicroFIPS Monitor Script
# Checks connectivity to FIPS service on VPS1

VPS1_IP="66.92.204.38"
VPS1_PORT="2121"

# Check if nc (netcat) is available
if ! command -v nc &> /dev/null; then
    echo "ERROR: nc (netcat) not installed" >&2
    exit 1
fi

# Test connectivity to VPS1 FIPS service
if nc -z -w 5 "$VPS1_IP" "$VPS1_PORT" &> /dev/null; then
    # Connection successful
    exit 0
else
    # Connection failed
    echo "FIPS UNREACHABLE: $VPS1_IP:$VPS1_PORT not responding" >&2
    exit 1
fi