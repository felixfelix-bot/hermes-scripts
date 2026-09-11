#!/usr/bin/env python3
"""Tests for nosigner NIP-04 support (nak's NIP-46 client uses NIP-04, not NIP-44).

Coverage:
  * Real cross-implementation vector produced by `nak encrypt --nip04`
    (Go implementation) decrypted by nosigner with AES-256-CBC.
  * nosigner nip04_encrypt output decrypted by `nak decrypt --nip04`.
  * Raw-x ECDH (NIP-04/NIP-44 secret) vs coincurve's SHA256(compressed) ecdh().
  * Content-format detection (nip04 / nip44 / plain).
  * Nip46Handler.decrypt_request + _make_response scheme round-trip.
  * End-to-end handler path: NIP-04 encrypted 'connect', then two sign_events
    in a row (regression guard for the "already connected" hang).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nosigner  # noqa: E402

# ── Fixed test keys (never used in production) ─────────────────────────────
ALICE_PRIV = bytes.fromhex("11" * 32)  # nak / client side
ALICE_PUB = "4f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"
BOB_PRIV = bytes.fromhex("22" * 32)  # nosigner side
BOB_PUB = "466d7fcae563e5cb09a0d1870bb580344804617879a14949cf22285f1bae3f27"

# Produced by: nak encrypt --nip04 -p <BOB_PUB> --sec <ALICE_PRIV> test
NAK_NIP04_CIPHERTEXT = "ELzzZ4uTaD5932drf7orSg==?iv=SmVCFZi3JVKd1adsq87qRw=="
NAK_PLAINTEXT = "test"


@pytest.fixture()
def handler(tmp_path):
    state = nosigner.BunkerState(db_path=tmp_path / "state.db")
    h = nosigner.Nip46Handler(BOB_PRIV, state)
    h.set_active_secret("s3cret")
    return h


# ── ECDH ───────────────────────────────────────────────────────────────────


def test_ecdh_shared_x_is_bare_x_coordinate():
    """NIP-04 key = raw x of d*P, NOT coincurve's SHA256(compressed point)."""
    shared = nosigner.ecdh_shared_x(BOB_PRIV, bytes.fromhex(ALICE_PUB))
    assert len(shared) == 32

    # Independent computation
    ref = nosigner.coincurve.PublicKey(
        bytes([0x02]) + bytes.fromhex(ALICE_PUB)
    ).multiply(BOB_PRIV)
    assert shared == ref.format(compressed=True)[1:]

    # The bug we are fixing: coincurve's ecdh() hashes the point.
    hashed = nosigner.coincurve.PrivateKey(BOB_PRIV).ecdh(
        bytes([0x02]) + bytes.fromhex(ALICE_PUB)
    )
    assert hashed != shared


def test_ecdh_is_symmetric():
    a = nosigner.ecdh_shared_x(ALICE_PRIV, bytes.fromhex(BOB_PUB))
    b = nosigner.ecdh_shared_x(BOB_PRIV, bytes.fromhex(ALICE_PUB))
    assert a == b


# ── Real nak vector ────────────────────────────────────────────────────────


def test_decrypts_real_nak_nip04_ciphertext():
    """Go (nak) encrypts → nosigner decrypts."""
    pt = nosigner.nip04_decrypt(NAK_NIP04_CIPHERTEXT, BOB_PRIV, bytes.fromhex(ALICE_PUB))
    assert pt == NAK_PLAINTEXT


def test_nip04_ciphertext_without_question_mark_is_rejected():
    with pytest.raises(ValueError):
        nosigner._parse_nip04("ELzzZ4uTaD5932drf7orSg==")


def test_nip04_bad_iv_length_rejected():
    with pytest.raises(ValueError):
        nosigner._parse_nip04("ELzzZ4uTaD5932drf7orSg==?iv=AAAA")


@pytest.mark.skipif(shutil.which("nak") is None, reason="nak not installed")
def test_nosigner_encrypt_is_readable_by_nak():
    """nosigner encrypts → Go (nak) decrypts."""
    ct = nosigner.nip04_encrypt("round trip", BOB_PRIV, bytes.fromhex(ALICE_PUB))
    assert "?iv=" in ct
    out = subprocess.run(
        ["nak", "decrypt", "--nip04", "-p", BOB_PUB, "--sec", ALICE_PRIV.hex(), ct],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "round trip"


def test_nip04_encrypt_decrypt_roundtrip_long_and_unicode():
    for msg in ["", "a" * 200, "ünïcödé ✓ nostr", "x" * 15]:  # 15 → full pad block
        ct = nosigner.nip04_encrypt(msg, BOB_PRIV, bytes.fromhex(ALICE_PUB))
        assert nosigner.nip04_decrypt(ct, BOB_PRIV, bytes.fromhex(ALICE_PUB)) == msg


# ── Detection ──────────────────────────────────────────────────────────────


def test_detect_encryption():
    nip04 = nosigner.nip04_encrypt("hello", BOB_PRIV, bytes.fromhex(ALICE_PUB))
    assert nosigner.detect_encryption(nip04) == "nip04"

    conv_key = nosigner.get_conversation_key(ALICE_PRIV, bytes.fromhex(BOB_PUB))
    nip44 = nosigner.nip44_encrypt("hello", conv_key)
    assert nosigner.detect_encryption(nip44) == "nip44"

    assert nosigner.detect_encryption('["id","connect",["s3cret"]]') == "plain"
    assert nosigner.detect_encryption("") == "plain"


def test_detect_encryption_nip04_is_not_mistaken_for_nip44():
    """The original bug: NIP-04 payload decoded to version byte 166 ('Unsupported NIP-44 version')."""
    nip04 = nosigner.nip04_encrypt("test", BOB_PRIV, bytes.fromhex(ALICE_PUB))
    ct_b64 = nip04.split("?")[0]
    assert base64.b64decode(ct_b64)[0] != 0x02  # would break NIP-44 version check
    assert nosigner.detect_encryption(nip04) == "nip04"


# ── decrypt_request ────────────────────────────────────────────────────────


def test_decrypt_request_nip04(handler):
    payload = json.dumps(["req-1", "get_public_key", []])
    content = nosigner.nip04_encrypt(payload, ALICE_PRIV, bytes.fromhex(BOB_PUB))
    parsed, mode = handler.decrypt_request(content, ALICE_PUB)
    assert mode == "nip04"
    assert parsed == ["req-1", "get_public_key", []]


def test_decrypt_request_nip44(handler):
    payload = json.dumps(["req-2", "ping", []])
    conv_key = nosigner.get_conversation_key(ALICE_PRIV, bytes.fromhex(BOB_PUB))
    content = nosigner.nip44_encrypt(payload, conv_key)
    parsed, mode = handler.decrypt_request(content, ALICE_PUB)
    assert mode == "nip44"
    assert parsed == ["req-2", "ping", []]


def test_decrypt_request_plain(handler):
    parsed, mode = handler.decrypt_request('["req-3","ping",[]]', ALICE_PUB)
    assert mode == "plain"
    assert parsed[1] == "ping"


def test_decrypt_request_garbage_raises(handler):
    with pytest.raises(ValueError):
        handler.decrypt_request("!!!not-base64!!!", ALICE_PUB)


# ── Responses mirror the request scheme ────────────────────────────────────


def test_response_uses_nip04_when_request_was_nip04(handler):
    handler._client_enc_mode[ALICE_PUB] = "nip04"
    resp = handler._make_response("req-1", "ok", "pong", ALICE_PUB)
    assert "?iv=" in resp["content"]
    decoded = json.loads(
        nosigner.nip04_decrypt(resp["content"], ALICE_PRIV, bytes.fromhex(BOB_PUB))
    )
    assert decoded == ["req-1", "ok", "pong"]
    assert resp["tags"] == [["p", ALICE_PUB]]


def test_response_uses_nip44_when_request_was_nip44(handler):
    handler._client_enc_mode[ALICE_PUB] = "nip44"
    resp = handler._make_response("req-2", "ok", "pong", ALICE_PUB)
    conv_key = nosigner.get_conversation_key(ALICE_PRIV, bytes.fromhex(BOB_PUB))
    assert json.loads(nosigner.nip44_decrypt(resp["content"], conv_key)) == [
        "req-2",
        "ok",
        "pong",
    ]


# ── End-to-end handler path (NIP-04, like nak) ─────────────────────────────


def _nip04_request(req_id: str, method: str, params: list) -> dict:
    content = nosigner.nip04_encrypt(
        json.dumps([req_id, method, params]), ALICE_PRIV, bytes.fromhex(BOB_PUB)
    )
    return {
        "id": "e" * 64,
        "pubkey": ALICE_PUB,
        "kind": nosigner.NIP_46_KIND,
        "tags": [["p", BOB_PUB]],
        "content": content,
        "created_at": 1,
        "sig": "0" * 128,
    }


def _decode_nip04_response(event: dict) -> list:
    plaintext = nosigner.nip04_decrypt(
        event["content"], ALICE_PRIV, bytes.fromhex(BOB_PUB)
    )
    return json.loads(plaintext)


def _assert_signature_is_valid_schnorr(sig_hex: str, template: dict) -> None:
    """Signature must be a 64-byte BIP-340 Schnorr signature over the event id.

    Regression guard for nosigner emitting coincurve's 65-byte recoverable
    *ECDSA* signature, which every Nostr client rejects ("invalid signature").
    Verified with the independent Go implementation (``nak verify``) when nak is
    on PATH; the length check always runs.
    """
    assert len(sig_hex) == 128, f"sig is {len(sig_hex)} hex chars, want 128"
    if shutil.which("nak") is None:
        return
    event_id = nosigner.compute_event_id(
        BOB_PUB, template["created_at"], template["kind"], template["tags"],
        template["content"],
    ).hex()
    event = {
        "id": event_id,
        "pubkey": BOB_PUB,
        "created_at": template["created_at"],
        "kind": template["kind"],
        "tags": template["tags"],
        "content": template["content"],
        "sig": sig_hex,
    }
    proc = subprocess.run(
        ["nak", "verify"], input=json.dumps(event), capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        f"nak verify rejected nosigner signature: {proc.stdout}{proc.stderr}"
    )


def test_make_event_sig_is_bip340_and_nak_verifies_it():
    """nosigner's event signature must pass an independent Go implementation."""
    event = nosigner.make_event(BOB_PRIV, 1, "bip340 check", [], 1700000000)
    _assert_signature_is_valid_schnorr(event["sig"], event)
    assert len(event["sig"]) == 128


def test_connect_then_two_signs_over_nip04(handler):
    """nak flow: connect, sign, sign again (no 'already connected' hang)."""
    connect = asyncio.run(
        handler.handle_request(_nip04_request("c1", "connect", ["s3cret"]))
    )
    assert connect is not None
    assert _decode_nip04_response(connect)[1] == "ok"
    assert handler.state.is_authorized(ALICE_PUB)

    template = {
        "kind": 1,
        "content": "hello from nak",
        "tags": [],
        "created_at": 1700000000,
    }
    first = asyncio.run(
        handler.handle_request(_nip04_request("s1", "sign_event", [template]))
    )
    assert first is not None
    sig1 = _decode_nip04_response(first)[2]
    assert len(sig1) == 128
    _assert_signature_is_valid_schnorr(sig1, template)

    second = asyncio.run(
        handler.handle_request(_nip04_request("s2", "sign_event", [template]))
    )
    assert second is not None, "second sign must not fail (already-connected bug)"
    body2 = _decode_nip04_response(second)
    assert body2[1] == "ok"
    assert len(body2[2]) == 128
    # BIP-340 nonces may carry aux randomness, so the two signatures need not be
    # byte-identical — but both must be valid signatures over the same event id.
    _assert_signature_is_valid_schnorr(body2[2], template)


def test_connect_with_wrong_secret_is_rejected_over_nip04(handler):
    resp = asyncio.run(handler.handle_request(_nip04_request("c2", "connect", ["nope"])))
    assert resp is not None
    body = _decode_nip04_response(resp)
    assert body[1] == "error"
    assert not handler.state.is_authorized(ALICE_PUB)


def test_unauthorized_sign_is_rejected_over_nip04(handler):
    resp = asyncio.run(
        handler.handle_request(
            _nip04_request("s3", "sign_event", [{"kind": 1, "content": "x"}])
        )
    )
    body = _decode_nip04_response(resp)
    assert body[1] == "error"


def test_ping_over_nip04(handler):
    handler.state.add_authorized_key(ALICE_PUB)
    resp = asyncio.run(handler.handle_request(_nip04_request("p1", "ping", [])))
    assert _decode_nip04_response(resp)[2] == "pong"


def test_nip04_encrypt_method_over_nip04(handler):
    handler.state.add_authorized_key(ALICE_PUB)
    resp = asyncio.run(
        handler.handle_request(
            _nip04_request("n1", "nip04_encrypt", ["secret note", ALICE_PUB])
        )
    )
    out = _decode_nip04_response(resp)
    assert out[1] == "ok"
    # The encrypted blob targets ALICE_PUB, so alice can read it back.
    assert (
        nosigner.nip04_decrypt(out[2], ALICE_PRIV, bytes.fromhex(BOB_PUB))
        == "secret note"
    )


def test_nip04_decrypt_method_over_nip04(handler):
    handler.state.add_authorized_key(ALICE_PUB)
    blob = nosigner.nip04_encrypt("from alice", ALICE_PRIV, bytes.fromhex(BOB_PUB))
    resp = asyncio.run(
        handler.handle_request(_nip04_request("n2", "nip04_decrypt", [blob, ALICE_PUB]))
    )
    assert _decode_nip04_response(resp)[2] == "from alice"


def test_event_from_nak_kind4_style_junk_does_not_crash(handler):
    """Unsupported/broken content must be swallowed (returns None), never raise."""
    event = _nip04_request("x1", "ping", [])
    event["content"] = "garbage"
    assert asyncio.run(handler.handle_request(event)) is None
