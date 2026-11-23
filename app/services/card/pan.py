"""PAN encryption at rest (AES-256-GCM). The PAN exists only inside card-service.

The key comes from ``CARD_PAN_KEY`` (base64, 32 bytes). Stored form:
``v1:<base64(nonce || ciphertext+tag)>``; the card_id is bound as associated data, so a
ciphertext copied onto another card's row does not decrypt.
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key() -> bytes:
    raw = os.environ.get("CARD_PAN_KEY", "")
    key = base64.b64decode(raw) if raw else b""
    if len(key) != 32:
        raise RuntimeError("CARD_PAN_KEY must be 32 bytes, base64-encoded")
    return key


def encrypt(pan: str, card_id: uuid.UUID) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key()).encrypt(nonce, pan.encode(), card_id.bytes)
    return "v1:" + base64.b64encode(nonce + ct).decode()


def decrypt(stored: str, card_id: uuid.UUID) -> str:
    version, _, payload = stored.partition(":")
    if version != "v1":
        raise ValueError("unknown PAN encryption version")
    raw = base64.b64decode(payload)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], card_id.bytes).decode()
