"""auth-service HTTP API."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from services.auth import passwords
from services.auth.models import login_attempts, refresh_tokens, users
from shared import clock
from shared.errors import AppError, ErrorBody
from shared.security import Customer, require_customer
from shared.settings import get_settings

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)

ERRORS = {401: {"model": ErrorBody}, 422: {"model": ErrorBody}}

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, description="The customer's username")
    password: str = Field(min_length=1, max_length=256, description="The customer's password")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256, description="Current refresh token")


class TokenResponse(BaseModel):
    access_token: str = Field(description="RS256 JWT, valid for 15 minutes")
    token_type: str = Field("Bearer", description="Always Bearer")
    expires_in: int = Field(description="Access token lifetime in seconds")
    refresh_token: str = Field(description="Opaque token, valid 7 days, rotates on every use")
    refresh_expires_in: int = Field(description="Refresh token lifetime in seconds")
    customer_id: uuid.UUID


class MeResponse(BaseModel):
    customer_id: uuid.UUID
    username: str
    display_name: str


def normalize(username: str) -> str:
    return username.strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _runtime(request: Request):
    return request.app.state.runtime


async def issue_tokens(request: Request, session, customer_id: uuid.UUID, family_id: uuid.UUID):
    settings = get_settings()
    key = request.app.state.signing_key
    now = clock.now()
    claims = {
        "sub": str(customer_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TTL).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    access = jwt.encode(claims, key.private_key, algorithm="RS256", headers={"kid": key.kid})
    refresh = "rt_" + secrets.token_urlsafe(32)
    await session.execute(
        refresh_tokens.insert().values(
            token_hash=_hash_token(refresh),
            family_id=family_id,
            customer_id=customer_id,
            issued_at=now,
            expires_at=now + REFRESH_TTL,
        )
    )
    return TokenResponse(
        access_token=access,
        expires_in=int(ACCESS_TTL.total_seconds()),
        refresh_token=refresh,
        refresh_expires_in=int(REFRESH_TTL.total_seconds()),
        customer_id=customer_id,
    )


@router.post(
    "/v1/auth/login",
    summary="Log in",
    description=(
        "Exchanges a username and password for a 15-minute access token and a 7-day refresh "
        "token. Five failed attempts for a username (known or not) lock it for 15 minutes "
        "(423 ACCOUNT_LOCKED). Unknown usernames and wrong passwords both return 401 "
        "INVALID_CREDENTIALS, so the response does not reveal which usernames exist."
    ),
    response_model=TokenResponse,
    responses={**ERRORS, 423: {"model": ErrorBody, "description": "Account locked"}},
)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    db = _runtime(request).db
    username = normalize(body.username)
    now = clock.now()
    outcome: str
    result: TokenResponse | None = None
    locked_until = None
    async with db.transaction() as s:
        # Lock this username's counter row so concurrent attempts are counted exactly.
        await s.execute(
            insert(login_attempts)
            .values(username=username, failed_count=0, updated_at=now)
            .on_conflict_do_nothing()
        )
        attempt = (
            await s.execute(
                sa.select(login_attempts)
                .where(login_attempts.c.username == username)
                .with_for_update()
            )
        ).one()
        failed = attempt.failed_count
        if attempt.locked_until is not None:
            if now < attempt.locked_until:
                outcome, locked_until = "locked", attempt.locked_until
            else:
                failed = 0  # the lockout has passed: start counting again
        if attempt.locked_until is None or now >= attempt.locked_until:
            user = (await s.execute(sa.select(users).where(users.c.username == username))).first()
            ok = await passwords.verify_async(body.password, user.password_hash if user else None)
            if ok and user is not None:
                outcome = "ok"
                await s.execute(
                    login_attempts.update()
                    .where(login_attempts.c.username == username)
                    .values(failed_count=0, locked_until=None, updated_at=now)
                )
                result = await issue_tokens(request, s, user.customer_id, uuid.uuid4())
            else:
                outcome = "failed"
                failed += 1
                await s.execute(
                    login_attempts.update()
                    .where(login_attempts.c.username == username)
                    .values(
                        failed_count=failed,
                        locked_until=now + LOCKOUT if failed >= MAX_FAILED_LOGINS else None,
                        updated_at=now,
                    )
                )
    # Raise only after the transaction commits, so failed attempts are always counted.
    if outcome == "locked":
        raise AppError(
            423,
            "ACCOUNT_LOCKED",
            "Too many failed logins. Try again later.",
            {"locked_until": locked_until.isoformat()},
        )
    if outcome == "failed":
        raise AppError(401, "INVALID_CREDENTIALS", "Username or password is incorrect")
    assert result is not None
    return result


@router.post(
    "/v1/auth/refresh",
    summary="Refresh tokens",
    description=(
        "Exchanges a refresh token for a new access token and a new refresh token. The old "
        "refresh token stops working. Presenting an already-used refresh token is treated as "
        "theft: the whole token family is revoked (401 REFRESH_TOKEN_REUSED)."
    ),
    response_model=TokenResponse,
    responses=ERRORS,
)
async def refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    db = _runtime(request).db
    now = clock.now()
    error: tuple[str, str] | None = None
    result: TokenResponse | None = None
    async with db.transaction() as s:
        row = (
            await s.execute(
                sa.select(refresh_tokens)
                .where(refresh_tokens.c.token_hash == _hash_token(body.refresh_token))
                .with_for_update()
            )
        ).first()
        if row is None or row.revoked_at is not None:
            error = ("REFRESH_TOKEN_INVALID", "Refresh token is not valid")
        elif row.used_at is not None:
            await s.execute(
                refresh_tokens.update()
                .where(refresh_tokens.c.family_id == row.family_id)
                .where(refresh_tokens.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            error = ("REFRESH_TOKEN_REUSED", "Refresh token was already used; session revoked")
        elif row.expires_at <= now:
            error = ("REFRESH_TOKEN_EXPIRED", "Refresh token has expired")
        else:
            await s.execute(
                refresh_tokens.update()
                .where(refresh_tokens.c.token_hash == row.token_hash)
                .values(used_at=now)
            )
            result = await issue_tokens(request, s, row.customer_id, row.family_id)
    if error:
        raise AppError(401, *error)
    assert result is not None
    return result


@router.post(
    "/v1/auth/logout",
    summary="Log out",
    description=(
        "Revokes the refresh token and every token in its family. Always 204, even for an "
        "unknown token, so the endpoint cannot be used to test tokens. The access token stays "
        "valid until it expires (at most 15 minutes)."
    ),
    status_code=204,
    responses={422: {"model": ErrorBody}},
)
async def logout(body: RefreshRequest, request: Request) -> Response:
    db = _runtime(request).db
    async with db.transaction() as s:
        family = await s.scalar(
            sa.select(refresh_tokens.c.family_id).where(
                refresh_tokens.c.token_hash == _hash_token(body.refresh_token)
            )
        )
        if family is not None:
            await s.execute(
                refresh_tokens.update()
                .where(refresh_tokens.c.family_id == family)
                .where(refresh_tokens.c.revoked_at.is_(None))
                .values(revoked_at=clock.now())
            )
    return Response(status_code=204)


@router.get(
    "/v1/auth/me",
    summary="Who am I",
    description="Returns the customer identified by the bearer access token.",
    response_model=MeResponse,
    responses={401: {"model": ErrorBody}},
)
async def me(request: Request, customer: Customer = Depends(require_customer)) -> MeResponse:
    async with _runtime(request).db.session() as s:
        user = (
            await s.execute(sa.select(users).where(users.c.customer_id == customer.customer_id))
        ).first()
    if user is None:
        raise AppError(401, "TOKEN_INVALID", "Customer no longer exists")
    return MeResponse(
        customer_id=user.customer_id, username=user.username, display_name=user.display_name
    )


@router.get(
    "/.well-known/jwks.json",
    summary="JSON Web Key Set",
    description="Public keys for verifying access tokens. Services cache it by kid.",
    tags=["auth"],
)
async def jwks(request: Request) -> dict:
    return {"keys": [request.app.state.signing_key.jwk()]}
