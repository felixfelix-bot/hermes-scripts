#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
# proxy-restart-tonight.sh — one-shot off-peak restart of zai-proxy (2026-08-16 03:10 IST)
# Picks up the PPQ policy engine + daily caps committed to zai_proxy.py (04a6ce8).
# Safe window: in-flight worker calls retry through localhost:9099 after restart.
exec /bin/systemctl --user restart zai-proxy.service
