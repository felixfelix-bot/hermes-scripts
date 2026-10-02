#!/usr/bin/env python3
"""dispatch-gate model-aware probe + failure classification (regression test).

Bug being pinned (measured 2026-10-02):
  `zai-quota-gate.sh` (-> dispatch-gate.sh) answered `ALLOW via fallback-probe`
  while the pool was dead for the model the caller was about to use. The probe
  was hardcoded to `deepseek-v4-flash` and `curl -f` threw the 503 body away, so
  every failure looked identical ("no-provider"). A watchdog built on the gate
  dispatched a worker pinned to glm-5.2 straight into a dead router.

Contract asserted here:
  1. no model given            -> legacy behaviour, unchanged.
  2. --model / DISPATCH_GATE_MODEL -> probes THAT model.
  3. "all providers exhausted" -> BLOCK capacity (transient)
  4. "no lane declares"        -> BLOCK no-lane  (config fault, never retries)
  5. a served model            -> ALLOW, even when the model-agnostic K1 verdict
                                  says can_dispatch=false.

Stub proxy is a local HTTPServer; no network and no real router involved.

Run from the repo root:  python3 tests/test_dispatch_gate_model_aware.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(REPO, "dispatch-gate.sh")

GOOD = {"data": [{"id": "stub"}], "object": "list"}
DISPATCH_GATE_DEAD = {
    "can_dispatch": False,
    "reason": "all lanes priced out (+inf): no deliverable lane",
    "all_inf": True,
    "quota_state": {"friend": {"used_pct": 0, "locked": True},
                    "ours": {"used_pct": 100, "locked": True}},
}
DISPATCH_GATE_OK = dict(DISPATCH_GATE_DEAD, can_dispatch=True)
# can_dispatch=true + friend locked = the FLAPPING state in which the old gate
# answered ALLOW from its generic probe while the caller's model was dead.
DISPATCH_GATE_FLAP = {
    "can_dispatch": True,
    "reason": "cheap capacity momentarily viable",
    "all_inf": False,
    "quota_state": {"friend": {"used_pct": 0, "locked": True},
                    "ours": {"used_pct": 100, "locked": True}},
}
DISPATCH_GATE_FRIEND_OK = {
    "can_dispatch": True,
    "reason": "friend unlocked",
    "all_inf": False,
    "quota_state": {"friend": {"used_pct": 0, "locked": False},
                    "ours": {"used_pct": 100, "locked": True}},
}


class Handler(BaseHTTPRequestHandler):
    dispatch_gate = DISPATCH_GATE_DEAD
    # model -> (http_code, body)
    models = {}

    def log_message(self, *a):  # keep the test output clean
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, GOOD)
        elif self.path.startswith("/v1/dispatch_gate"):
            self._send(200, Handler.dispatch_gate)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        model = req.get("model", "?")
        code, body = Handler.models.get(model, (503, {"error": "unknown model"}))
        self._send(code, body)


def run_gate(env_extra, args=()):
    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env.update(env_extra)
    p = subprocess.run(["bash", GATE, *args], capture_output=True, text=True, env=env, timeout=90)
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def case(name, ok, detail):
    print(("PASS  " if ok else "FAIL  ") + name + ("  :: " + detail if detail else ""))
    return ok


def main():
    Handler.models = {
        # generic probe model: SERVES -> this is what made the old gate lie
        "deepseek-v4-flash": (200, {"choices": [{"message": {"content": "OK"}}]}),
        # Realistic bodies copied from live router responses (2026-10-02). Note
        # the capacity body carries `"no_candidate_lane": false` -- the field
        # NAME must never be substring-matched or a drain reads as a config fault.
        "model-capacity": (503, {"error": "all providers exhausted (flat router)",
                                 "capacity_exhausted": True,
                                 "model": "model-capacity",
                                 "candidates_tried": ["ours", "opencode_go"],
                                 "candidates_considered": ["ours", "friend", "ollama_cloud", "opencode_go"],
                                 "candidates_skipped_undeliverable": ["friend", "ollama_cloud"],
                                 "no_candidate_lane": False,
                                 "candidates_last_resort": ["ours", "opencode_go"]}),
        "model-nolane": (503, {"error": "no lane declares this model (flat router)",
                               "capacity_exhausted": False,
                               "no_candidate_lane": True,
                               "candidates_tried": [],
                               "candidates_considered": []}),
        "model-ok": (200, {"choices": [{"message": {"content": "OK"}}]}),
    }
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    results = []
    try:
        # ---- (A) K1 blocks the legacy path when no cheap lane has headroom ----
        Handler.dispatch_gate = DISPATCH_GATE_DEAD
        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base})
        results.append(case("legacy: can_dispatch=false -> BLOCK capacity (K1 unchanged)",
                            rc == 1 and "BLOCK capacity" in out, f"rc={rc} {out.strip()[:100]}"))

        # ---- (B) THE BUG: flapping can_dispatch=true + dead caller model ----
        # Old gate: generic probe of deepseek-v4-flash succeeded -> ALLOW, and a
        # worker pinned to model-capacity died on its first call.
        Handler.dispatch_gate = DISPATCH_GATE_FLAP
        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base})
        results.append(case("legacy (flapping): generic probe ALLOWs - the false green, documented",
                            rc == 0 and "ALLOW" in out, f"rc={rc} {out.strip()[:100]}"))

        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base, "DISPATCH_GATE_MODEL": "model-capacity"})
        results.append(case("FIX: same state, caller's model named -> BLOCK capacity",
                            rc == 1 and "BLOCK capacity" in out, f"rc={rc} {out.strip()[:110]}"))

        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base}, ("--model", "model-capacity"))
        results.append(case("FIX: --model form -> BLOCK capacity",
                            rc == 1 and "BLOCK capacity" in out, f"rc={rc} {out.strip()[:110]}"))

        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base, "DISPATCH_GATE_MODEL": "model-nolane"})
        results.append(case("FIX: no lane declares -> BLOCK no-lane (config fault, not capacity)",
                            rc == 1 and "BLOCK no-lane" in out,
                            f"rc={rc} {out.strip()[:110]}"))

        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base, "DISPATCH_GATE_MODEL": "model-ok"})
        results.append(case("FIX: named model that serves -> ALLOW (K1 does not override ground truth)",
                            rc == 0 and "ALLOW" in out, f"rc={rc} {out.strip()[:100]}"))

        # ---- (C) friend unlocked short-circuits without probing (unchanged path)
        Handler.dispatch_gate = DISPATCH_GATE_FRIEND_OK
        rc, out = run_gate({"DISPATCH_GATE_PROXY_URL": base, "DISPATCH_GATE_MODEL": "model-capacity"})
        results.append(case("friend unlocked -> ALLOW without a probe (unchanged)",
                            rc == 0 and "ALLOW" in out, f"rc={rc} {out.strip()[:100]}"))
    finally:
        srv.shutdown()

    print()
    print(f"{sum(1 for r in results if r)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
