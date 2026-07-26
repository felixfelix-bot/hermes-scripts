#!/usr/bin/env python3
"""
nosigner — NIP-46 Remote Signer (Bunker) Daemon

Implements the NIP-46 remote signer protocol over Nostr relays.
Replaces nak's buggy bunker daemon which had "already connected" hangs
and empty-method errors.

Usage:
    python3 nosigner.py --sec <nsec> [--relay <wss://...>] [--daemon]
    python3 nosigner.py --bunker-url <bunker://url> [--one-shot]

Protocol: NIP-46 (Remote Signer)
Encryption: NIP-44
Events: kind 24133 (connect request/response)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import coincurve
import websockets.asyncio.client as ws_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Constants ──────────────────────────────────────────────────────────────

NIP_46_KIND = 24133
PING_INTERVAL = 30  # seconds between pings
RECONNECT_BASE = 1  # base seconds for exponential backoff
RECONNECT_MAX = 60
MAX_PENDING = 128  # max in-flight requests per relay

HOME = Path.home()
STATE_DIR = HOME / ".hermes" / "state" / "bunker"
LOG_DIR = HOME / ".hermes" / "logs" / "nosigner"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────

logger = logging.getLogger("nosigner")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.setLevel(level)
    logger.addHandler(handler)
    # Also log to file
    fh = logging.FileHandler(LOG_DIR / "nosigner.log")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


# ── Crypto / NIP-44 ────────────────────────────────────────────────────────

# NIP-44 uses AES-256-GCM with a specific key derivation
# See https://github.com/nostr-protocol/nips/blob/master/44.md


def _hkdf_derive(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HMAC-based Extract-and-Expand Key Derivation (NIP-44 spec)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(ikm)


def nip44_encrypt(plaintext: str, conversation_key: bytes) -> str:
    """NIP-44 v2 encrypt. Returns base64 payload."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives import hashes
    import hmac as hmac_mod
    import hashlib

    # NIP-44 v2: derive per-message key
    # 1. HKDF-Expand(PRK=conversation_key, info="nip44-v2", L=32)
    hkdf_expand = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"nip44-v2",
    )
    chacha_key = hkdf_expand.derive(conversation_key)

    # 2. Generate random nonce (32 bytes)
    nonce = os.urandom(32)

    # 3. HMAC-SHA256(chacha_key, nonce) -> actual encryption key
    h = hmac_mod.new(chacha_key, nonce, hashlib.sha256)
    enc_key = h.digest()

    # 4. Encrypt with ChaCha20Poly1305, nonce=zero(12)
    cipher = ChaCha20Poly1305(enc_key)
    pt_bytes = plaintext.encode("utf-8")
    ct = cipher.encrypt(b"\x00" * 12, pt_bytes, None)

    # 5. Payload: version(1) + nonce(32) + ciphertext+mac
    payload = bytes([0x02]) + nonce + ct
    return base64.b64encode(payload).decode()


def nip44_decrypt(ciphertext_b64: str, conversation_key: bytes) -> str:
    """NIP-44 v2 decrypt. Returns plaintext string."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives import hashes
    import hmac as hmac_mod
    import hashlib

    payload = base64.b64decode(ciphertext_b64)

    # Parse NIP-44 v2 payload: version(1) + nonce(32) + ciphertext+mac(rest)
    if len(payload) < 1:
        raise ValueError("Empty payload")
    version = payload[0]
    if version != 0x02:
        raise ValueError(f"Unsupported NIP-44 version: {version}")

    nonce = payload[1:33]
    ct_with_mac = payload[33:]

    # Derive per-message key
    # 1. HKDF-Expand(PRK=conversation_key, info="nip44-v2", L=32)
    hkdf_expand = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"nip44-v2",
    )
    chacha_key = hkdf_expand.derive(conversation_key)

    # 2. HMAC-SHA256(chacha_key, nonce) -> actual encryption key
    h = hmac_mod.new(chacha_key, nonce, hashlib.sha256)
    enc_key = h.digest()

    # 3. Decrypt with ChaCha20Poly1305, nonce=zero(12)
    cipher = ChaCha20Poly1305(enc_key)
    pt = cipher.decrypt(b"\x00" * 12, ct_with_mac, None)
    return pt.decode("utf-8")


def get_conversation_key(privkey: bytes, pubkey_xonly: bytes) -> bytes:
    """Derive NIP-44 conversation key from private key and remote public key.

    Uses ECDH then HKDF as specified in NIP-44.
    The pubkey_xonly is the 32-byte x-only public key (Nostr format).
    For ECDH we need the compressed 33-byte format - we use 02 prefix
    (even y) since ECDH with x-only is valid for both y parities on the curve.
    """
    # ECDH: reconstruct compressed pubkey (02 prefix for the x coordinate)
    compressed_pubkey = bytes([0x02]) + pubkey_xonly
    our_sk = coincurve.PrivateKey(privkey)
    shared_point = our_sk.ecdh(compressed_pubkey)
    # HKDF derivation
    return _hkdf_derive(
        ikm=shared_point,
        salt=b"nip44-v2",
        info=b"nip44-conversation-key",
        length=32,
    )


# ── Key utilities ──────────────────────────────────────────────────────────

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_decode(s: str) -> tuple[str, list[int]]:
    """Minimal bech32 decoder for npub/nsec."""
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1:
        raise ValueError("Invalid bech32: no separator")
    hrp = s[:pos]
    data = [CHARSET.index(c) for c in s[pos + 1 :]]
    return hrp, data


def convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for v in data:
        if v < 0 or (v >> frombits):
            raise ValueError("Invalid value")
        acc = ((acc << frombits) | v) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("Invalid padding")
    return ret


def decode_npub(s: str) -> bytes:
    """Decode npub1... to 32-byte hex public key."""
    hrp, data = bech32_decode(s)
    if hrp not in ("npub", "nsec"):
        raise ValueError(f"Expected npub or nsec, got {hrp}")
    payload = convertbits(data[:-6], 5, 8)  # exclude checksum
    # First byte is version (must be 0 for npub)
    if payload[0] != 0:
        raise ValueError(f"Unsupported version byte: {payload[0]}")
    return bytes(payload[1:])


def decode_nsec(s: str) -> bytes:
    """Decode nsec1... to 32-byte private key."""
    hrp, data = bech32_decode(s)
    if hrp != "nsec":
        raise ValueError(f"Expected nsec, got {hrp}")
    payload = convertbits(data[:-6], 5, 8)
    if payload[0] != 0:
        raise ValueError(f"Unsupported version byte: {payload[0]}")
    return bytes(payload[1:])


def pubkey_to_hex(pk: coincurve.PublicKey) -> str:
    return pk.format().hex()


def privkey_to_pubkey(priv: bytes) -> bytes:
    """Get 32-byte x-only public key from private key (Nostr format)."""
    sk = coincurve.PrivateKey(priv)
    # coincurve format() returns 33-byte compressed (prefix 02/03 + x)
    # Nostr uses x-only (just the 32-byte x coordinate)
    return sk.public_key.format()[1:]


def sign_event(privkey: bytes, event_hash: bytes) -> str:
    """Sign an event hash (32 bytes) with the private key.
    Returns hex-encoded signature (65 bytes = r||s||v for Nostr).
    """
    sk = coincurve.PrivateKey(privkey)
    sig = sk.sign_recoverable(event_hash)
    return sig.hex()


def hashlib_sha256(msg: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(msg).digest()


def compute_event_id(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> bytes:
    """Compute the NIP-01 event id (SHA256 of serialized event)."""
    import hashlib

    serialized = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).digest()


# ── Bunker URL parsing ─────────────────────────────────────────────────────


@dataclass
class BunkerConfig:
    """Parsed bunker:// URL."""

    signer_pubkey: str  # hex
    relays: list[str]
    secret: str
    client_pubkey: str | None = None  # optional, the authorized client key


def parse_bunker_url(url: str) -> BunkerConfig:
    """Parse a bunker:// URL."""
    if not url.startswith("bunker://"):
        raise ValueError(f"Invalid bunker URL: {url}")

    rest = url[len("bunker://") :]
    # rest is signer_pubkey or npub
    qpos = rest.find("?")
    if qpos < 0:
        raise ValueError(f"No query params in bunker URL: {url}")

    pubkey_raw = rest[:qpos]
    # Decode npub if needed
    if pubkey_raw.startswith("npub1"):
        signer_pubkey = decode_npub(pubkey_raw).hex()
    elif len(pubkey_raw) == 64:
        signer_pubkey = pubkey_raw
    elif len(pubkey_raw) == 66 and pubkey_raw.startswith(("02", "03", "04")):
        # Compressed/uncompressed secp256k1 format - strip prefix
        signer_pubkey = pubkey_raw[2:]
    else:
        raise ValueError(f"Unrecognized pubkey format ({len(pubkey_raw)} chars): {pubkey_raw[:16]}...")

    # Parse query params
    from urllib.parse import parse_qs

    qs = rest[qpos + 1 :]
    params = parse_qs(qs, keep_blank_values=True)

    relays: list[str] = []
    for r in params.get("relay", []):
        if r:
            relays.append(r)

    secret = params.get("secret", [""])[0]

    return BunkerConfig(
        signer_pubkey=signer_pubkey,
        relays=relays or ["wss://relay.nsec.app", "wss://nostr.oxtr.dev", "wss://relay.primal.net"],
        secret=secret,
    )


# ── State / persistence ─────────────────────────────────────────────────────


class BunkerState:
    """Persistent state for the bunker — authorized keys, config."""

    def __init__(self, db_path: Path = STATE_DIR / "state.db"):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        c = self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS authorized_keys (
                pubkey_hex TEXT PRIMARY KEY,
                label TEXT DEFAULT '',
                added_at INTEGER NOT NULL,
                last_used_at INTEGER
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bunker_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def add_authorized_key(self, pubkey_hex: str, label: str = "") -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO authorized_keys (pubkey_hex, label, added_at) VALUES (?, ?, ?)",
            (pubkey_hex, label, int(time.time())),
        )
        conn.commit()

    def is_authorized(self, pubkey_hex: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM authorized_keys WHERE pubkey_hex = ?", (pubkey_hex,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE authorized_keys SET last_used_at = ? WHERE pubkey_hex = ?",
                (int(time.time()), pubkey_hex),
            )
            conn.commit()
            return True
        return False

    def list_authorized_keys(self) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT pubkey_hex, label, added_at, last_used_at FROM authorized_keys ORDER BY added_at DESC"
        ).fetchall()
        return [
            {
                "pubkey_hex": r[0],
                "label": r[1],
                "added_at": r[2],
                "last_used_at": r[3],
            }
            for r in rows
        ]

    def save_config(self, key: str, value: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO bunker_config (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()

    def get_config(self, key: str, default: str = "") -> str:
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM bunker_config WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Event building ─────────────────────────────────────────────────────────


def make_event(
    privkey: bytes,
    kind: int,
    content: str,
    tags: list[list[str]] | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Create and sign a Nostr event."""
    pubkey_hex = privkey_to_pubkey(privkey).hex()
    created_at = created_at or int(time.time())
    tags = tags or []
    content_str = content if isinstance(content, str) else json.dumps(content)

    event_id = compute_event_id(pubkey_hex, created_at, kind, tags, content_str)
    sig = sign_event(privkey, event_id)

    return {
        "id": event_id.hex(),
        "pubkey": pubkey_hex,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content_str,
        "sig": sig,
    }


# ── Relay connection manager ───────────────────────────────────────────────


class RelayPool:
    """Manages WebSocket connections to multiple relays."""

    def __init__(self, relays: list[str]):
        self.relays = relays
        self._conns: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._sub_id: str | None = None

    async def start(self) -> None:
        for r in self.relays:
            self._queues[r] = asyncio.Queue()
            self._conns[r] = asyncio.create_task(self._run_relay(r))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._conns.values():
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns.values(), return_exceptions=True)
        self._conns.clear()

    async def publish(self, event: dict[str, Any]) -> None:
        msg = json.dumps(["EVENT", event])
        for q in self._queues.values():
            await q.put(msg)

    async def subscribe(self, kinds: list[int], authors: list[str] | None = None, p_tags: list[str] | None = None) -> str:
        sub_id = uuid.uuid4().hex[:12]
        self._sub_id = sub_id
        filt: dict[str, Any] = {"kinds": kinds}
        if authors is not None:
            filt["authors"] = authors
        if p_tags is not None:
            filt["#p"] = p_tags
        sub = json.dumps(["REQ", sub_id, filt])
        logger.debug(f"Subscribing with filter: {filt}")
        for q in self._queues.values():
            await q.put(sub)
        return sub_id

    async def close_subscription(self) -> None:
        if self._sub_id:
            msg = json.dumps(["CLOSE", self._sub_id])
            for q in self._queues.values():
                await q.put(msg)
            self._sub_id = None

    async def receive(self) -> tuple[str, dict[str, Any]]:
        """Receive one event, returns (relay_url, event_dict)."""
        return await self._incoming.get()

    async def _run_relay(self, relay_url: str) -> None:
        backoff = RECONNECT_BASE
        while not self._stop.is_set():
            try:
                logger.debug(f"Connecting to {relay_url}...")
                async with ws_client.connect(relay_url, ping_interval=20) as ws:
                    logger.info(f"Connected to {relay_url}")
                    backoff = RECONNECT_BASE
                    send_task = asyncio.create_task(self._send_loop(relay_url, ws))
                    recv_task = asyncio.create_task(self._recv_loop(relay_url, ws))
                    done, _ = await asyncio.wait(
                        [send_task, recv_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in done:
                        try:
                            exc = t.exception()
                            if exc:
                                logger.warning(f"{relay_url} task failed: {exc}")
                        except asyncio.CancelledError:
                            pass
                    send_task.cancel()
                    recv_task.cancel()
                    await asyncio.gather(send_task, recv_task, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Connection to {relay_url} failed: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    async def _send_loop(self, relay_url: str, ws) -> None:
        queue = self._queues[relay_url]
        try:
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1)
                    await ws.send(msg)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    async def _recv_loop(self, relay_url: str, ws) -> None:
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    logger.debug(f"{relay_url} recv: {data[0] if data else '?'}")
                    if data[0] == "EVENT":
                        await self._incoming.put((relay_url, data[2]))
                    elif data[0] == "EOSE":
                        logger.debug(f"{relay_url} EOSE for sub {data[1]}")
                    elif data[0] == "NOTICE":
                        logger.warning(f"{relay_url} NOTICE: {data[1] if len(data) > 1 else '?'}")
                    elif data[0] == "OK":
                        logger.debug(f"{relay_url} OK: {data[1:] if len(data) > 1 else ''}")
                except (json.JSONDecodeError, IndexError) as e:
                    logger.debug(f"{relay_url} parse error: {e}")
                    continue
        except asyncio.CancelledError:
            pass


# ── NIP-46 Request/Response handler ────────────────────────────────────────


class Nip46Handler:
    """Handles NIP-46 remote signer requests."""

    def __init__(self, privkey: bytes, state: BunkerState):
        self.privkey = privkey
        self.pubkey_hex = privkey_to_pubkey(privkey).hex()
        self.state = state
        self._pending_pings: dict[str, float] = {}
        self._conversation_keys: dict[str, bytes] = {}  # client_pubkey -> conv_key
        self._active_secret: str | None = None

    def set_active_secret(self, secret: str) -> None:
        self._active_secret = secret

    def get_conversation_key_for(self, client_pubkey_hex: str) -> bytes:
        """Get or derive the conversation key for a client."""
        if client_pubkey_hex not in self._conversation_keys:
            client_pk_bytes = bytes.fromhex(client_pubkey_hex)
            self._conversation_keys[client_pubkey_hex] = get_conversation_key(
                self.privkey, client_pk_bytes
            )
        return self._conversation_keys[client_pubkey_hex]

    async def handle_request(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Handle an incoming NIP-46 request event. Returns response event or None."""
        raw_content = event.get("content", "")
        client_pubkey = event.get("pubkey", "")

        # NIP-46 requests are NIP-44 encrypted. Try to decrypt first.
        content = None
        if raw_content.startswith("["):
            # Unencrypted (rare, some test clients)
            try:
                content = json.loads(raw_content)
            except json.JSONDecodeError:
                logger.warning(f"Invalid unencrypted content: {raw_content[:100]}")
                return None
        else:
            # Encrypted — decrypt with NIP-44
            try:
                conv_key = self.get_conversation_key_for(client_pubkey)
                decrypted = nip44_decrypt(raw_content, conv_key)
                content = json.loads(decrypted)
                logger.debug(f"Decrypted request from {client_pubkey[:16]}...: method={content[1] if len(content) > 1 else '?'}")
            except Exception as e:
                logger.warning(f"Failed to decrypt request from {client_pubkey[:16]}...: {e}")
                return None

        if not isinstance(content, list) or len(content) < 2:
            return None

        req_id = content[0] if isinstance(content[0], str) else ""
        method = content[1]
        params = content[2] if len(content) > 2 else []
        if not isinstance(params, list):
            params = [params]

        # Check authorization for all methods except 'connect'
        client_pubkey = event.get("pubkey", "")

        if method != "connect":
            if not self.state.is_authorized(client_pubkey):
                logger.warning(f"Unauthorized method '{method}' from {client_pubkey}")
                return self._make_response(req_id, "error", "Unauthorized", client_pubkey)

        logger.info(f"  Method: {method} from {client_pubkey[:16]}...")

        handlers = {
            "connect": self._handle_connect,
            "sign_event": self._handle_sign_event,
            "get_public_key": self._handle_get_public_key,
            "ping": self._handle_ping,
            "nip04_encrypt": self._handle_nip04_encrypt,
            "nip04_decrypt": self._handle_nip04_decrypt,
            "nip44_encrypt": self._handle_nip44_encrypt,
            "nip44_decrypt": self._handle_nip44_decrypt,
            "get_relays": self._handle_get_relays,
        }

        handler = handlers.get(method)
        if handler is None:
            logger.warning(f"Unknown method: {method}")
            return self._make_response(req_id, "error", f"Unknown method: {method}", client_pubkey)

        try:
            result = await handler(req_id, params, client_pubkey)
            return result
        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return self._make_response(req_id, "error", str(e), client_pubkey)

    def _make_response(
        self, req_id: str, result_type: str, result: Any, client_pubkey: str
    ) -> dict[str, Any]:
        """Create a NIP-46 response event."""
        response_content = json.dumps([req_id, result_type, result])
        # Encrypt the response with NIP-44
        if client_pubkey and client_pubkey in self._conversation_keys:
            conv_key = self._conversation_keys[client_pubkey]
            encrypted = nip44_encrypt(response_content, conv_key)
            content = encrypted
        else:
            content = response_content

        return make_event(
            privkey=self.privkey,
            kind=NIP_46_KIND,
            content=content,
            tags=[["p", client_pubkey]] if client_pubkey else [],
        )

    # ── Method handlers ────────────────────────────────────────────────

    async def _handle_connect(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'connect' — verify secret and authorize the client."""
        if len(params) < 1:
            return self._make_response(req_id, "error", "Missing params", client_pubkey)

        secret = params[0] if len(params) > 0 else ""
        # Also check the event content for the secret (some clients embed it)
        # Verify secret
        if self._active_secret and secret != self._active_secret:
            logger.warning(f"Connect failed: bad secret from {client_pubkey[:16]}...")
            return self._make_response(req_id, "error", "Unauthorized", client_pubkey)

        # Authorize this client
        self.state.add_authorized_key(client_pubkey)

        # Derive conversation key
        conv_key = self.get_conversation_key_for(client_pubkey)

        # Respond with "ok" — client is now authorized
        return self._make_response(
            req_id,
            "ok",
            {"id": req_id, "result": True},
            client_pubkey,
        )

    async def _handle_sign_event(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'sign_event' — sign a nostr event template."""
        if not params:
            return self._make_response(req_id, "error", "Missing event template", client_pubkey)

        template = params[0] if isinstance(params[0], dict) else json.loads(params[0])
        kind = template.get("kind", 1)
        content = template.get("content", "")
        tags = template.get("tags", [])
        created_at = template.get("created_at", int(time.time()))

        event = make_event(self.privkey, kind, content, tags, created_at)
        logger.info(f"  Signed event kind={kind} id={event['id'][:16]}...")

        return self._make_response(req_id, "ok", event["sig"], client_pubkey)

    async def _handle_get_public_key(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'get_public_key' — return signer's pubkey."""
        return self._make_response(req_id, "ok", self.pubkey_hex, client_pubkey)

    async def _handle_ping(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'ping' — respond with 'pong'."""
        return self._make_response(req_id, "ok", "pong", client_pubkey)

    async def _handle_nip04_encrypt(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'nip04_encrypt'."""
        return self._make_response(req_id, "error", "NIP-04 not implemented, use NIP-44", client_pubkey)

    async def _handle_nip04_decrypt(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'nip04_decrypt'."""
        return self._make_response(req_id, "error", "NIP-04 not implemented, use NIP-44", client_pubkey)

    async def _handle_nip44_encrypt(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'nip44_encrypt'."""
        if len(params) < 2:
            return self._make_response(req_id, "error", "Need plaintext and pubkey", client_pubkey)
        plaintext = params[0]
        target_pubkey_hex = params[1]
        try:
            conv_key = get_conversation_key(self.privkey, bytes.fromhex(target_pubkey_hex))
            encrypted = nip44_encrypt(plaintext, conv_key)
            return self._make_response(req_id, "ok", encrypted, client_pubkey)
        except Exception as e:
            return self._make_response(req_id, "error", str(e), client_pubkey)

    async def _handle_nip44_decrypt(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'nip44_decrypt'."""
        if len(params) < 2:
            return self._make_response(req_id, "error", "Need ciphertext and pubkey", client_pubkey)
        ciphertext = params[0]
        sender_pubkey_hex = params[1]
        try:
            conv_key = get_conversation_key(self.privkey, bytes.fromhex(sender_pubkey_hex))
            decrypted = nip44_decrypt(ciphertext, conv_key)
            return self._make_response(req_id, "ok", decrypted, client_pubkey)
        except Exception as e:
            return self._make_response(req_id, "error", str(e), client_pubkey)

    async def _handle_get_relays(
        self, req_id: str, params: list, client_pubkey: str
    ) -> dict[str, Any]:
        """Handle 'get_relays'."""
        relays = self.state.get_config("relays", "")
        relay_list = json.loads(relays) if relays else []
        return self._make_response(req_id, "ok", relay_list, client_pubkey)


# ── Main daemon ────────────────────────────────────────────────────────────


class BunkerDaemon:
    """Main NIP-46 bunker daemon."""

    def __init__(self, privkey: bytes, relays: list[str], secret: str | None = None):
        self.privkey = privkey
        self.pubkey_hex = privkey_to_pubkey(privkey).hex()
        self.relays = relays
        self.secret = secret
        self.state = BunkerState()
        self.handler = Nip46Handler(privkey, self.state)
        self.pool = RelayPool(relays)
        self._sub_id: str | None = None

        if secret:
            self.handler.set_active_secret(secret)
            self.state.save_config("active_secret", secret)

        self.state.save_config("relays", json.dumps(relays))

    async def start(self) -> None:
        logger.info(f"Starting bunker daemon")
        logger.info(f"  Pubkey: {self.pubkey_hex}")
        logger.info(f"  Relays: {', '.join(self.relays)}")
        logger.info(f"  Secret: {'set' if self.secret else 'not set'}")

        bunker_url = self._make_bunker_url()
        logger.info(f"  Bunker URL: {bunker_url}")

        await self.pool.start()
        self._sub_id = await self.pool.subscribe(
            kinds=[NIP_46_KIND],
            # Receive all kind 24133 events, filter by p-tag in handler
        )
        logger.info("Listening for NIP-46 requests...")

        try:
            while True:
                relay_url, event = await self.pool.receive()
                if event.get("kind") != NIP_46_KIND:
                    continue

                # Check if we should handle this event
                # For now, handle all kind 24133 events
                response = await self.handler.handle_request(event)
                if response:
                    await self.pool.publish(response)
                    logger.debug(f"  Response published for event {event.get('id', '')[:16]}...")
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutting down bunker daemon...")
        if self._sub_id:
            await self.pool.close_subscription()
        await self.pool.stop()
        self.state.close()
        logger.info("Bunker daemon stopped.")

    def _make_bunker_url(self) -> str:
        """Generate the bunker:// URL for this signer."""
        from urllib.parse import urlencode

        params = {}
        for r in self.relays:
            params.setdefault("relay", []).append(r)
        if self.secret:
            params["secret"] = self.secret
        qs = urlencode(params, doseq=True)
        return f"bunker://{self.pubkey_hex}?{qs}"


# ── CLI entry point ────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIP-46 Remote Signer (Bunker) Daemon")
    parser.add_argument("--sec", help="nsec private key for the signer")
    parser.add_argument("--bunker-url", help="bunker:// URL (parsed for config)")
    parser.add_argument("--daemon", action="store_true", help="Run as persistent daemon")
    parser.add_argument("--one-shot", action="store_true", help="Handle one request then exit")
    parser.add_argument("--relay", action="append", dest="relays", help="Relay URLs")
    parser.add_argument("--secret", help="Connection secret for bunker URL")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    privkey: bytes | None = None
    relays: list[str] = []
    secret: str | None = None

    if args.sec:
        if args.sec.startswith("nsec1"):
            privkey = decode_nsec(args.sec)
        elif len(args.sec) == 64:
            privkey = bytes.fromhex(args.sec)
        else:
            logger.error("Invalid private key format")
            sys.exit(1)

    if args.bunker_url:
        config = parse_bunker_url(args.bunker_url)
        if not privkey:
            logger.error("--sec required with --bunker-url")
            sys.exit(1)
        relays = config.relays
        secret = config.secret
    elif args.relays:
        relays = args.relays
    else:
        relays = ["wss://nostr.oxtr.dev", "wss://relay.nsec.app", "wss://relay.primal.net"]

    if args.secret:
        secret = args.secret

    if not privkey:
        logger.error("No private key provided. Use --sec <nsec>")
        sys.exit(1)

    daemon = BunkerDaemon(privkey, relays, secret)

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        logger.info("Exiting")


if __name__ == "__main__":
    main()
