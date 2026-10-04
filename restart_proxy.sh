#!/bin/bash
# One-shot restart of zai-proxy.service to load P0-1 _OLLAMA_CLOUD_KEYS change.
export XDG_RUNTIME_DIR=/run/user/$(id -u)
sleep 2
systemctl --user restart zai-proxy.service
sleep 6
echo "is-active: $(systemctl --user is-active zai-proxy.service)"
echo "pid: $(pgrep -f zai_proxy.py | head -1)"