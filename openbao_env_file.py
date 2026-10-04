#!/usr/bin/env python3
"""openbao_env_file.py — materialize an OpenBao secret as a systemd env file.

ADR-020 consumer cutover: a systemd oneshot runs this before a service starts,
writing the secret's fields as ``KEY=value`` lines to a root/user-only file in
/run. The service then reads it via ``EnvironmentFile=`` — so provider keys come
from OpenBao (in-memory fetch, short-TTL token) instead of a plaintext ``.env``.

Reuses ``fleet_secret`` (AppRole login + in-memory fetch). On any failure it
leaves the previous file untouched and exits non-zero, so the service falls back
to whatever it already had.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "scripts"))

import fleet_secret as fs  # noqa: E402


def _load_openbao_env() -> None:
    """Fill OPENBAO_* from ~/.hermes/openbao/env (written by role 72) if unset.

    Makes this runnable from a systemd oneshot with no pre-populated env.
    """
    p = HERMES / "openbao" / "env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="logical secret name (fleet/<name>)")
    ap.add_argument("--out", required=True, help="output env file path")
    ap.add_argument("--field-prefix", default="", help="optional export prefix")
    ap.add_argument("--exclude-prefix", action="append", default=[],
                    help="skip fields starting with this prefix (repeatable)")
    args = ap.parse_args(argv)

    _load_openbao_env()
    data = fs.get_secret(args.name, node=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)

    lines = []
    for k, v in sorted(data.items()):
        if any(k.startswith(_p) for _p in args.exclude_prefix):
            continue
        sval = str(v)
        if "\n" in sval:
            sval = sval.replace("\n", "\\n")
        lines.append(f"{args.field_prefix}{k}={sval}\n")

    fd, tmp = tempfile.mkstemp(dir=str(out.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(lines)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)  # atomic; a failed fetch never truncates the old file
    except Exception:
        os.unlink(tmp)
        raise
    print(f"wrote {len(lines)} var(s) to {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"openbao_env_file: {exc}", file=sys.stderr)
        raise SystemExit(1)
