"""Password hashing with the standard library's scrypt (no extra dependency).

Format: ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``. Hashing runs in a thread so the
event loop keeps serving other requests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2**14, 8, 1, 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


# Verified against when the username is unknown, so both paths cost the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


async def hash_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_async(password: str, encoded: str | None) -> bool:
    ok = await asyncio.to_thread(verify_password, password, encoded or DUMMY_HASH)
    return ok and encoded is not None
