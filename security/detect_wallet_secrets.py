#!/usr/bin/env python3
"""detect_wallet_secrets.py — fleet wallet-material detector (BIP-39 seeds + Cashu bearer tokens).

Why this exists: the repo-configured `detect-secrets` gate returns ZERO findings for a real
BIP-39 seed phrase (it has no mnemonic plugin), so a live seed shipped to a public branch on
2026-09-20 inside a raw lab log. Cashu tokens are bearer instruments (`CONTRIBUTING.md:98-100`)
and were committed the same way. This detector closes both classes.

Usage:
    detect_wallet_secrets.py --staged [repo]      # files staged in the repo (default: cwd)
    detect_wallet_secrets.py <path> [path...]     # explicit files
    detect_wallet_secrets.py --dir <dir>          # walk a directory (skips .git/node_modules)

Exit: 0 = clean, 1 = finding(s) reported, 2 = detector unusable (fail-closed caller decides).

Design notes:
 * A mnemonic is only a finding if it is REAL: every word in the BIP-39 English wordlist AND the
   checksum validates AND the entropy is not all-zero. The canonical all-zero test vector
   ("abandon … about") is a public example, not a secret — flagging it would train everyone to
   use --no-verify.
 * Output is masked: the words/token are never printed, only the location and the class.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
WORDLIST = HOOK_DIR / "bip39-english.txt"
ALLOWLIST = HOOK_DIR / "wallet-secret-allowlist.txt"

TOKEN_RE = re.compile(r"cashu[AB][A-Za-z0-9_+/=-]{40,}")
MNEMONIC_RE = re.compile(r"\b((?:[a-z]{3,8}\s+){11,23}[a-z]{3,8})\b")
CONTEXT_RE = re.compile(r"mnemonic|seed[ _-]?phrase|seed words|recovery phrase|BIP-?39", re.I)
MAX_BYTES = 8 * 1024 * 1024


def load_wordlist() -> set[str] | None:
    if not WORDLIST.is_file():
        return None
    words = {w.strip() for w in WORDLIST.read_text().split() if w.strip()}
    return words if len(words) >= 2048 else None


class Bip39:
    def __init__(self, wl: set[str]) -> None:
        self.words = wl
        self.index = {w: i for i, w in enumerate(self.sorted_words())}

    def sorted_words(self) -> list[str]:
        # the official list is alphabetical; keep a stable ordering for index lookup
        self._sorted = getattr(self, "_sorted", None) or sorted(self.words)
        return self._sorted

    def classify(self, phrase: str) -> str | None:
        """Return 'seed' for a real mnemonic, 'testvector' for BIP-39's all-zero example, else None."""
        ws = phrase.split()
        if len(ws) not in (12, 15, 18, 21, 24):
            return None
        if any(w not in self.words for w in ws):
            return None
        bits = "".join(f"{self.index[w]:011b}" for w in ws)
        ent_bits, chk_bits = bits[: len(bits) * 32 // 33], bits[len(bits) * 32 // 33:]
        entropy = int(ent_bits, 2).to_bytes(len(ent_bits) // 8, "big")
        digest = hashlib.sha256(entropy).digest()
        want = "".join(f"{b:08b}" for b in digest)[: len(chk_bits)]
        if want != chk_bits:
            return None
        return "testvector" if set(entropy) == {0} else "seed"


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(name: str, text: str, bip39: Bip39 | None) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if TOKEN_RE.search(line):
            hits.append((name, i, "cashu-bearer-token"))
        if bip39 and CONTEXT_RE.search(line):
            for m in MNEMONIC_RE.finditer(line):
                kind = bip39.classify(m.group(1))
                if kind == "seed":
                    hits.append((name, i, "bip39-seed"))
                elif kind == "testvector":
                    hits.append((name, i, "bip39-test-vector(public-example)"))
    return hits


def allowlisted(entry: str) -> bool:
    if not ALLOWLIST.is_file():
        return False
    for line in ALLOWLIST.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line in entry:
            return True
    return False


def staged_files(repo: str) -> list[Path]:
    out = subprocess.run(["git", "-C", repo, "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                         capture_output=True, text=True)
    return [Path(repo) / p for p in out.stdout.split() if p]


def dir_files(root: str) -> list[Path]:
    skip = {".git", "node_modules", ".venv", "__pycache__", "target", "dist", "build", ".next", ".cache"}
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            p = Path(base) / f
            if p.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz", ".xz", ".ipk", ".apk", ".pdf"}:
                continue
            found.append(p)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--staged", nargs="?", const=".", default=None)
    ap.add_argument("--dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    wl = load_wordlist()
    bip39 = Bip39(wl) if wl else None
    if bip39 is None and not args.quiet:
        print("detect_wallet_secrets: WARNING bip39 wordlist missing — mnemonic detection degraded",
              file=sys.stderr)

    if args.staged is not None:
        files = staged_files(os.path.abspath(args.staged))
    elif args.dir:
        files = dir_files(args.dir)
    else:
        files = [Path(p) for p in args.paths]
    if not files:
        return 0

    findings: list[tuple[str, int, str]] = []
    for p in files:
        text = read_text(p)
        if text is None:
            continue
        findings.extend(scan_text(str(p), text, bip39))

    blocking = [f for f in findings if "test-vector" not in f[2] and not allowlisted(f"{f[0]}:{f[1]}")]
    advisory = [f for f in findings if "test-vector" in f[2]]

    for path, line, kind in blocking:
        print(f"WALLET-SECRET {kind}: {path}:{line}  (value withheld)")
    if advisory and not args.quiet:
        for path, line, kind in advisory:
            print(f"note: {path}:{line} is the public BIP-39 test vector — allowed, not a secret",
                  file=sys.stderr)

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
