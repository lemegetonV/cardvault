"""Caller authentication shared by every bank service.

* Customers: RS256 access tokens issued by auth-service. Each service verifies tokens
  itself against the JWKS (cached; re-fetched when an unknown ``kid`` appears). ``iss``,
  ``aud`` and ``exp`` are checked against the shared clock, not the wall clock, so tests can
  expire a token by moving time (rule 37).
* Services: one ``X-Service-Key`` per caller (rule 40). Internal endpoints reject customer
  JWTs and customer endpoints reject service keys, both with 401.
* Test controls: ``X-Test-Key``, only when ``TEST_MODE=true``.
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Request
from jwt import PyJWK

from shared import clock
from shared.errors import AppError
from shared.settings import get_settings

log = logging.getLogger(__name__)

BEARER = {"WWW-Authenticate": "Bearer"}
REQUIRED_CLAIMS = ("sub", "iss", "aud", "exp", "iat", "jti")


@dataclass(frozen=True)
class Customer:
    """The authenticated customer. ``customer_id`` always comes from the token."""

    customer_id: uuid.UUID
    token_id: str
    claims: dict[str, Any]


KeyProvider = Callable[[str], Awaitable[Any | None]]


class JwksCache:
    """Fetches and caches the auth-service JWKS. Unknown ``kid`` forces a re-fetch,
    at most once every ``min_refetch_seconds``, so key rotation needs no restart."""

    def __init__(self, url: str, min_refetch_seconds: float = 5.0) -> None:
        self.url = url
        self.min_refetch_seconds = min_refetch_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            data = resp.json()
        self._keys = {k["kid"]: PyJWK(k).key for k in data.get("keys", []) if "kid" in k}
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> Any | None:
        if kid not in self._keys and time.monotonic() - self._fetched_at > self.min_refetch_seconds:
            try:
                await self._fetch()
            except Exception:
                log.warning("JWKS fetch failed", exc_info=True)
        return self._keys.get(kid)


class TokenVerifier:
    def __init__(self, key_provider: KeyProvider, issuer: str, audience: str) -> None:
        self.key_provider = key_provider
        self.issuer = issuer
        self.audience = audience

    async def verify(self, token: str) -> Customer:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AppError(401, "TOKEN_INVALID", "Malformed token", headers=BEARER) from None
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise AppError(401, "TOKEN_INVALID", "Unsupported token header", headers=BEARER)
        key = await self.key_provider(header["kid"])
        if key is None:
            raise AppError(401, "TOKEN_INVALID", "Unknown signing key", headers=BEARER)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                # Time claims are checked below against the shared clock instead.
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": list(REQUIRED_CLAIMS),
                },
            )
        except jwt.PyJWTError as exc:
            raise AppError(401, "TOKEN_INVALID", f"Invalid token: {exc}", headers=BEARER) from None
        if int(claims["exp"]) <= int(clock.now().timestamp()):
            raise AppError(401, "TOKEN_EXPIRED", "Access token has expired", headers=BEARER)
        try:
            customer_id = uuid.UUID(str(claims["sub"]))
        except ValueError:
            raise AppError(401, "TOKEN_INVALID", "Invalid subject", headers=BEARER) from None
        return Customer(customer_id=customer_id, token_id=str(claims["jti"]), claims=claims)


_verifier: TokenVerifier | None = None


def configure_verifier(verifier: TokenVerifier) -> None:
    global _verifier
    _verifier = verifier


def default_verifier() -> TokenVerifier:
    s = get_settings()
    if not s.auth_jwks_url:
        raise RuntimeError("AUTH_JWKS_URL is not set")
    return TokenVerifier(JwksCache(s.auth_jwks_url).get, s.jwt_issuer, s.jwt_audience)


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def require_customer(request: Request) -> Customer:
    """FastAPI dependency for customer endpoints."""
    token = bearer_token(request)
    if token is None:
        raise AppError(401, "UNAUTHORIZED", "A customer bearer token is required", headers=BEARER)
    global _verifier
    if _verifier is None:
        _verifier = default_verifier()
    return await _verifier.verify(token)


def require_service(*allowed: str) -> Callable[[Request], Awaitable[str]]:
    """FastAPI dependency factory for internal endpoints. Returns the caller's name."""

    async def dependency(request: Request) -> str:
        presented = request.headers.get("x-service-key")
        if presented:
            for caller, key in get_settings().service_keys.items():
                if (not allowed or caller in allowed) and hmac.compare_digest(presented, key):
                    return caller
        raise AppError(401, "SERVICE_KEY_INVALID", "A valid X-Service-Key is required")

    return dependency


async def require_test_key(request: Request) -> None:
    s = get_settings()
    presented = request.headers.get("x-test-key", "")
    if not s.test_key or not hmac.compare_digest(presented, s.test_key):
        raise AppError(401, "TEST_KEY_INVALID", "A valid X-Test-Key is required")
