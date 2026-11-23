"""RS256 signing key: loaded from the DB, or generated and stored on first start."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.dialects.postgresql import insert

from services.auth.models import signing_keys
from shared.db import Database


@dataclass
class SigningKey:
    kid: str
    private_key: rsa.RSAPrivateKey

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        return self.private_key.public_key()

    def jwk(self) -> dict[str, Any]:
        data = json.loads(RSAAlgorithm.to_jwk(self.public_key))
        data.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return data


def _thumbprint(public_key: rsa.RSAPublicKey) -> str:
    """RFC 7638 JWK thumbprint, used as the ``kid``."""
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    canonical = json.dumps({k: jwk[k] for k in ("e", "kty", "n")}, separators=(",", ":"))
    return (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()
    )


def _load(pem: str) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key


async def load_or_create(db: Database) -> SigningKey:
    async with db.transaction() as s:
        row = (
            await s.execute(
                sa.select(signing_keys)
                .where(signing_keys.c.active.is_(True))
                .order_by(signing_keys.c.created_at)
                .limit(1)
            )
        ).first()
        if row is not None:
            return SigningKey(kid=row.kid, private_key=_load(row.private_pem))
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        kid = _thumbprint(private_key.public_key())
        await s.execute(
            insert(signing_keys).values(kid=kid, private_pem=pem).on_conflict_do_nothing()
        )
        return SigningKey(kid=kid, private_key=private_key)
