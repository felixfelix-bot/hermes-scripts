#!/usr/bin/env python3
"""fleet_notify.py — send a short message to the operator Signal group via the
running signal-cli daemon JSON-RPC (the method the fleet bridge uses)."""
import json
import sys
import urllib.request

GROUP = "V8tnIinI5Yh6wAqXj2vGa0PfJ27j6zHLgpeZJexODEA="
ACCOUNT = "+18102940908"
RPC = "http://127.0.0.1:8080/api/v1/rpc"


def send(msg: str) -> int:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": "fleet-notify", "method": "send",
        "params": {"account": ACCOUNT, "groupId": GROUP, "message": msg},
    }).encode()
    req = urllib.request.Request(RPC, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return 0
    except Exception as exc:  # best-effort
        print(f"notify failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(send(" ".join(sys.argv[1:])))
