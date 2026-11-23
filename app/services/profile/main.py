"""profile-service application: wiring, seed and test APIs."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import Field

from services.profile.api import ProfilePatch, ProfileView, publish_contact, view
from services.profile.api import router as api_router
from services.profile.models import RESET_TABLES, outbox, profiles
from shared import clock, seed_data
from shared.errors import AppError
from shared.outbox import OutboxRelay, kafka_event_handler
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_test_key
from shared.settings import get_settings


async def startup(runtime: Runtime) -> None:
    s = get_settings()
    runtime.relays.append(
        OutboxRelay(
            runtime.db, outbox, {"event": kafka_event_handler(runtime.producer, s.event_backoff)}
        )
    )


async def seed(runtime: Runtime) -> None:
    """Seed profiles directly. No contact.updated is published: enrollment-service seeds its
    own projection from the same constants (spec 16.7 rule 47)."""
    now = clock.now()
    rows = [
        {
            "customer_id": customer_id,
            "email": c.email,
            "email_verified": c.email_verified,
            "phone": c.phone,
            "phone_verified": c.phone_verified,
            "version": c.version,
            "created_at": now,
            "updated_at": now,
        }
        for customer_id, c in seed_data.CONTACTS.items()
    ]
    async with runtime.db.transaction() as s:
        await s.execute(profiles.insert(), rows)


test_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


class TestProfileRequest(ProfilePatch):
    customer_id: uuid.UUID
    email_verified: bool = True
    phone_verified: bool = True
    publish: bool = Field(False, description="Also publish contact.updated")


@test_router.post(
    "/profiles",
    summary="Create a profile (test support)",
    description="Used by the POST /test/customers orchestrator. Email and phone default to "
    "fictional values derived from the customer_id.",
    status_code=201,
    response_model=ProfileView,
)
async def create_profile(body: TestProfileRequest, request: Request) -> ProfileView:
    db = request.app.state.runtime.db
    now = clock.now()
    short = body.customer_id.hex[:8]
    async with db.transaction() as s:
        exists = await s.scalar(
            sa.select(sa.func.count())
            .select_from(profiles)
            .where(profiles.c.customer_id == body.customer_id)
        )
        if exists:
            raise AppError(409, "PROFILE_EXISTS", "A profile exists for this customer")
        await s.execute(
            profiles.insert().values(
                customer_id=body.customer_id,
                email=body.email or f"cust-{short}@example.com",
                email_verified=body.email_verified,
                phone=body.phone or f"+1555{int(short, 16) % 10_000_000:07d}",
                phone_verified=body.phone_verified,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            await s.execute(sa.select(profiles).where(profiles.c.customer_id == body.customer_id))
        ).one()
        if body.publish:
            await publish_contact(s, row)
    for relay in request.app.state.runtime.relays:
        relay.wake()
    return view(row)


app = create_app(
    ServiceSpec(
        name="profile",
        title="CardVault profile-service",
        description=(
            "Customer contact details for CardVault Bank: email and phone, their verification "
            "with one-time codes, and the contact.updated event that keeps wallet eligibility "
            "in step."
        ),
        routers=[api_router],
        test_routers=[test_router],
        uses_db=True,
        uses_kafka=True,
        reset_tables=RESET_TABLES,
        on_startup=[startup],
        on_seed=[seed],
    )
)
