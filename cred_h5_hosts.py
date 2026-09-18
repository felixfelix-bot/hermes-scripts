#!/usr/bin/env python3
"""cred_h5_hosts.py — CRED-H5 checks (a) and (b): the retired login literal is
rejected by every host, the rotated value is accepted.

Proof layers per host (all three where the host allows it):
  1. sshd policy   — password authentication disabled (keys-only, H8/H9)
  2. account store — crypt(retired, /etc/shadow hash) != hash  (PAM-level rejection,
                     true even at a physical console where sshd policy does not apply)
                     and crypt(current, /etc/shadow hash) == hash (rotation live)
  3. the wire      — one ssh password attempt with the retired value (1 attempt,
                     no lockout risk) must fail; a SUCCESS here is a live exposure
Plus: key login works and sudo -n works on CBW/DQ05 (H4 property), and the vault
value equals the value the fleet actually uses.

A literal never appears in argv, in the process table, or in this script's output:
the retired value is read from the needle table, the current value from the vault,
and sshpass reads them from a 0600 file (-f). Output carries ids, sha256/12
fingerprints and booleans only.

Exit 0 all proven, 1 a check FAILED (old value accepted somewhere, or the rotated
value not accepted), 3 no FAIL but at least one UNKNOWN (fail-closed report).
"""
from __future__ import annotations

import argparse
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import crypt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
DEFAULT_POLICY = os.path.join(HOME, ".git-hooks", "cred-h5-policy.json")
VAULT = os.path.join(HOME, "secrets", "secrets.kdbx")
MASTER_FILE = os.path.join(HOME, "secrets", ".vault-master")
LOCAL_ENV = os.path.join(HOME, ".hermes", ".env")

RESULTS: list[tuple[str, str, str]] = []   # (status, label, detail)


def rec(status: str, label: str, detail: str = ""):
    RESULTS.append((status, label, detail))
    print(f"  [{status:<7}] {label}" + (f" — {detail}" if detail else ""))


def fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def run(cmd: list[str], timeout: int = 30, stdin: str | None = None):
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def load_needles(policy: dict) -> dict[str, str]:
    path = os.path.expanduser(policy["needles"])
    out = {}
    for line in open(path, errors="replace"):
        if line.startswith("#") or not line.strip():
            continue
        rid, _, lit = line.rstrip("\n").partition("\t")
        if rid and lit:
            out[rid] = lit
    if not out:
        print("cred_h5_hosts: needle table empty", file=sys.stderr)
        sys.exit(2)
    return out


def vault_value(entry: str) -> str | None:
    """Read one password from the vault. The value is returned, never printed."""
    if not (os.path.exists(VAULT) and os.path.exists(MASTER_FILE)):
        return None
    master = open(MASTER_FILE).read().strip()
    p = run(["keepassxc-cli", "show", "-q", "-s", "-a", "password", VAULT, entry],
            timeout=60, stdin=master + "\n")
    out = p.stdout.strip().splitlines()
    return out[-1] if p.returncode == 0 and out else None


def shadow_hash(host: dict) -> str | None:
    user = host.get("login", "c03rad0r")
    if host["kind"] == "local":
        p = run(["sudo", "-n", "getent", "shadow", user], timeout=30)
    else:
        p = run(ssh_cmd(host) + ["sudo -n getent shadow " + user], timeout=45)
    if p.returncode != 0:
        return None
    parts = p.stdout.strip().split(":")
    return parts[1] if len(parts) > 1 and parts[1] not in ("*", "!", "!!", "") else None


def target_ip(host: dict) -> str:
    if host["kind"] == "local":
        return "127.0.0.1"
    return host["target"].split("@")[-1]


def ssh_base(host: dict) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", host["target"]]


def ssh_cmd(host: dict) -> list[str]:
    return ssh_base(host)


def sshd_policy(host: dict) -> str | None:
    cmd = (["sudo", "-n", "sshd", "-T"] if host["kind"] == "local"
           else ssh_cmd(host) + ["sudo -n sshd -T"])
    p = run(cmd, timeout=45)
    if p.returncode != 0:
        return None
    keep = []
    for line in p.stdout.splitlines():
        if line.lower().startswith(("passwordauthentication", "kbdinteractiveauthentication",
                                    "pubkeyauthentication")):
            keep.append(line.strip())
    return "; ".join(keep) if keep else None


def wire_probe(host: dict, value: str, label: str) -> tuple[str, str]:
    """One ssh password attempt. Returns (verdict, note).

    verdict PASS = the offered password authentication rejected the value;
    FAIL = the value was accepted (live exposure); UNKNOWN = no verdict.
    The note records whether sshd OFFERED password auth at all, because a
    rejection only proves the value is dead when password auth was on the table.
    """
    if not shutil.which("sshpass"):
        return "UNKNOWN", "sshpass missing"
    tmpd = tempfile.mkdtemp(prefix="cred-h5-pw-")
    os.chmod(tmpd, 0o700)
    pwfile = os.path.join(tmpd, "v")
    with open(pwfile, "w") as fh:
        fh.write(value)
    os.chmod(pwfile, 0o600)
    try:
        cmd = ["sshpass", "-f", pwfile, "ssh",
               "-o", "PreferredAuthentications=password",
               "-o", "PubkeyAuthentication=no",
               "-o", "NumberOfPasswordPrompts=1",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=8",
               f"{host.get('login', 'root')}@{target_ip(host)}",
               "true"]
        p = run(cmd, timeout=30)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    err = (p.stderr or "").lower()
    methods = ""
    m = re.search(r"permission denied \(([^)]*)\)", err)
    if m:
        methods = m.group(1)
    offered = "password" in methods
    if p.returncode == 0:
        return "FAIL", "accepted"
    if "permission denied" in err or "authentication failed" in err or "too many" in err:
        return "PASS", ("password auth offered and rejected" if offered
                        else f"rejected, but sshd offered only [{methods or 'unknown'}] "
                             "- rejection does not prove the value is dead")
    if "timed out" in err or "unreachable" in err or "refused" in err or p.returncode == 124:
        return "UNKNOWN", "host unreachable"
    return "UNKNOWN", (p.stderr or "").strip()[:80]


def check_host(host: dict, needles: dict[str, str], retired_id: str) -> None:
    name = host["name"]
    managed = host.get("managed", True)
    retired = needles.get(retired_id)
    print(f"\n== host {name} ({host['kind']}, {host.get('target', 'localhost')}) ==")
    if not retired:
        rec("UNKNOWN", f"{name}: retired literal", f"rule id {retired_id} absent from needle table")
        return
    rec("INFO", f"{name}: retired literal id={retired_id} sha256/12={fp(retired)}")
    new = vault_value(host["vault_entry"]) if host.get("vault_entry") else None

    # (1) sshd policy ---------------------------------------------------------
    if not managed:
        rec("INFO", f"{name}: sshd policy / account store",
            "not applicable - no key material or credential is held for this host "
            "(the wire probe below is the only available proof)")
    else:
        pol = sshd_policy(host)
        if pol is None:
            rec("UNKNOWN", f"{name}: sshd effective policy unreadable")
        elif "passwordauthentication no" in pol.lower():
            rec("PASS", f"{name}: password auth disabled (keys-only)", pol)
        else:
            rec("FAIL", f"{name}: password auth still ENABLED", pol)

        # (2) account store ---------------------------------------------------
        h = shadow_hash(host)
        if h is None:
            rec("UNKNOWN", f"{name}: /etc/shadow hash unreadable (crypt check skipped)")
        else:
            algo = h.split("$")[1] if h.startswith("$") else "unknown"
            old_ok = crypt.crypt(retired, h) == h
            rec("FAIL" if old_ok else "PASS",
                f"{name}: retired value vs account store (crypt/${algo})",
                "ACCEPTED — the host still authenticates the retired literal"
                if old_ok else "rejected (hash mismatch)")
            if not new:
                rec("UNKNOWN", f"{name}: rotated value from vault", "vault entry unreadable")
            else:
                rec("PASS" if crypt.crypt(new, h) == h else "FAIL",
                    f"{name}: rotated vault value vs account store",
                    f"sha256/12={fp(new)} → "
                    + ("accepted (PAM-level)" if crypt.crypt(new, h) == h else "NOT accepted"))

    # (3) the wire ------------------------------------------------------------
    st, note = wire_probe(host, retired, "retired")
    rec(st, f"{name}: ssh password auth with the retired value",
        {"PASS": "rejected on the wire (1 attempt) — " + note,
         "FAIL": "ACCEPTED — LIVE EXPOSURE, rotate now",
         "UNKNOWN": note}[st])
    if new and host["kind"] == "ssh":
        st2, note2 = wire_probe(host, new, "rotated")
        rec("INFO", f"{name}: ssh password auth with the rotated value",
            note2 + " (expected under keys-only: see account-store + key checks)")

    # (4) key login + sudo ----------------------------------------------------
    if host["kind"] == "ssh" and host.get("key_login", True):
        p = run(ssh_cmd(host) + ["echo KEY_LOGIN_OK; id -un"], timeout=45)
        rec("PASS" if "KEY_LOGIN_OK" in p.stdout else "FAIL",
            f"{name}: key-based login", p.stdout.strip()[:60] or p.stderr.strip()[:80])
        if host.get("sudo_nopasswd"):
            p = run(ssh_cmd(host) + ["sudo -n true && echo SUDO_OK"], timeout=45)
            rec("PASS" if "SUDO_OK" in p.stdout else "FAIL", f"{name}: sudo -n (NOPASSWD)")
    elif host["kind"] == "local":
        if host.get("sudo_nopasswd"):
            p = run(["sudo", "-n", "true"], timeout=20)
            rec("PASS" if p.returncode == 0 else "FAIL", f"{name}: sudo -n (NOPASSWD)")
        rec("INFO", f"{name}: local login identity", run(["id", "-un"]).stdout.strip())
    else:
        rec("INFO", f"{name}: key-based login", "no key material held for this host")

    # (5) vault == fleet-in-use value ----------------------------------------
    if host.get("env_key"):
        if host["kind"] == "local":
            text = open(LOCAL_ENV, errors="replace").read() if os.path.exists(LOCAL_ENV) else ""
        else:
            text = run(ssh_cmd(host) + [f"cat {host['remote_env']}"], timeout=45).stdout
        live = None
        for line in text.splitlines():
            if line.startswith(host["env_key"] + "="):
                live = line.split("=", 1)[1].strip().strip('"').strip("'")
        if new and live:
            rec("PASS" if new == live else "FAIL",
                f"{name}: vault value == env value ({host['env_key']})",
                f"vault sha256/12={fp(new)} env sha256/12={fp(live)}")
        else:
            rec("UNKNOWN", f"{name}: vault/env value comparison", "one side unavailable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=DEFAULT_POLICY)
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None, help="comma-separated host names")
    args = ap.parse_args()

    policy = json.load(open(args.policy))
    needles = load_needles(policy)
    retired_id = policy.get("host_credential_rule", "masterpwd")
    hosts = policy["hosts"]
    if args.only:
        want = {h.strip() for h in args.only.split(",")}
        hosts = [h for h in hosts if h["name"] in want]

    print(f"CRED-H5 host checks (a)+(b)   hosts={len(hosts)}  "
          f"retired rule={retired_id}  {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    for h in hosts:
        check_host(h, needles, retired_id)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    unknowns = [r for r in RESULTS if r[0] == "UNKNOWN"]
    print(f"\nhost verdict: PASS={sum(1 for r in RESULTS if r[0] == 'PASS')} "
          f"FAIL={len(fails)} UNKNOWN={len(unknowns)}")
    for st, label, detail in fails + unknowns:
        print(f"  {st}: {label} {detail}")
    if args.json:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "results": [{"status": s, "check": l, "detail": d} for s, l, d in RESULTS],
                   "fail": len(fails), "unknown": len(unknowns)},
                  open(args.json, "w"), indent=1)
    if fails:
        return 1
    return 3 if unknowns else 0


if __name__ == "__main__":
    sys.exit(main())
