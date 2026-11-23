"""auth-service application."""

from __future__ import annotations

import os
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from services.auth import keys, passwords
from services.auth.api import MeResponse, normalize
from services.auth.api import router as api_router
from services.auth.models import RESET_TABLES, users
from shared import clock, seed_ids
from shared.errors import AppError
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import TokenVerifier, configure_verifier, require_test_key
from shared.settings import get_settings


async def startup(runtime: Runtime) -> None:
    signing_key = await keys.load_or_create(runtime.db)
    app.state.signing_key = signing_key
    s = get_settings()

    # auth-service verifies its own tokens from memory instead of fetching its own JWKS.
    async def local_key(kid: str):
        return signing_key.public_key if kid == signing_key.kid else None

    configure_verifier(TokenVerifier(local_key, s.jwt_issuer, s.jwt_audience))


async def seed(runtime: Runtime) -> None:
    password = os.environ.get("SEED_USER_PASSWORD")
    if not password:
        raise AppError(500, "SEED_CONFIG_MISSING", "SEED_USER_PASSWORD is not set")
    now = clock.now()
    rows = [
        {
            "customer_id": customer_id,
            "username": username,
            "password_hash": await passwords.hash_async(password),
            "display_name": display_name,
            "created_at": now,
        }
        for username, (customer_id, display_name) in seed_ids.CUSTOMERS.items()
    ]
    async with runtime.db.transaction() as s:
        await s.execute(users.insert(), rows)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    customer_id: uuid.UUID | None = Field(None, description="Generated when omitted")
    display_name: str | None = Field(None, max_length=128)


test_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


@test_router.post(
    "/users",
    summary="Create a login (test support)",
    description="Used by the POST /test/customers orchestrator to create a fresh customer.",
    status_code=201,
    response_model=MeResponse,
)
async def create_user(body: CreateUserRequest, request: Request) -> MeResponse:
    username = normalize(body.username)
    customer_id = body.customer_id or uuid.uuid4()
    display_name = body.display_name or username
    async with request.app.state.runtime.db.transaction() as s:
        exists = await s.scalar(
            sa.select(sa.func.count())
            .select_from(users)
            .where(sa.or_(users.c.username == username, users.c.customer_id == customer_id))
        )
        if exists:
            raise AppError(409, "USER_EXISTS", "Username or customer_id already exists")
        await s.execute(
            users.insert().values(
                customer_id=customer_id,
                username=username,
                password_hash=await passwords.hash_async(body.password),
                display_name=display_name,
                created_at=clock.now(),
            )
        )
    return MeResponse(customer_id=customer_id, username=username, display_name=display_name)


app = create_app(
    ServiceSpec(
        name="auth",
        title="CardVault auth-service",
        description=(
            "Customer authentication for CardVault Bank: login with lockout, RS256 access "
            "tokens (15 min), rotating refresh tokens (7 days) and the JWKS other services "
            "use to verify tokens themselves."
        ),
        routers=[api_router],
        test_routers=[test_router],
        uses_db=True,
        uses_kafka=False,
        reset_tables=RESET_TABLES,
        on_startup=[startup],
        on_seed=[seed],
    )
)
