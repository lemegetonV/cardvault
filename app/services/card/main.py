"""card-service application: wiring, the enrollment.changed consumer, seed and test APIs."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from services.card import domain
from services.card.api import router as customer_router
from services.card.internal import router as internal_router
from services.card.models import RESET_TABLES, cards, outbox, wallet_tokens
from shared import clock, events, seed_data
from shared.kafka import Consumer
from shared.outbox import OutboxRelay, kafka_event_handler
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_test_key
from shared.settings import get_settings

CONSUMER_GROUP = "card-service"


async def apply_enrollment(session, payload: events.EnrollmentChangedPayload) -> None:
    """Fold one enrollment.changed event into the local token copy.

    Versions make it idempotent and order-safe: a row only moves forward. Tokens of the same
    enrolment other than the current one stop being ENROLLED (re-enrolment issues a new
    token, spec 16.1 rule 4).
    """
    now = clock.now()
    if payload.token_ref:
        stmt = insert(wallet_tokens).values(
            token_ref=payload.token_ref,
            card_id=payload.card_id,
            enrollment_id=payload.enrollment_id,
            status=payload.status,
            version=payload.version,
            updated_at=now,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[wallet_tokens.c.token_ref],
                set_={
                    "status": stmt.excluded.status,
                    "version": stmt.excluded.version,
                    "updated_at": stmt.excluded.updated_at,
                },
                where=wallet_tokens.c.version < stmt.excluded.version,
            )
        )
    await session.execute(
        wallet_tokens.update()
        .where(
            wallet_tokens.c.enrollment_id == payload.enrollment_id,
            wallet_tokens.c.token_ref != (payload.token_ref or ""),
            wallet_tokens.c.version < payload.version,
            wallet_tokens.c.status != "REMOVED",
        )
        .values(status="REMOVED", version=payload.version, updated_at=now)
    )


async def startup(runtime: Runtime) -> None:
    s = get_settings()
    runtime.relays.append(
        OutboxRelay(
            runtime.db, outbox, {"event": kafka_event_handler(runtime.producer, s.event_backoff)}
        )
    )

    async def handle(envelope: events.Envelope) -> None:
        async with runtime.db.transaction() as session:
            await apply_enrollment(session, envelope.payload)

    runtime.consumers.append(
        Consumer(
            bootstrap=s.kafka_bootstrap,
            group_id=CONSUMER_GROUP,
            topics=[events.ENROLLMENT_CHANGED],
            handler=handle,
            dlq_topic=events.ENROLLMENT_DLQ,
            producer=runtime.producer,
            backoff=s.event_backoff,
        )
    )


async def seed(runtime: Runtime) -> None:
    now = clock.now()
    pans = seed_data.seed_pans()
    async with runtime.db.transaction() as s:
        for card in seed_data.CARDS:
            await domain.create_card(
                s,
                card_id=card.card_id,
                customer_id=card.customer_id,
                product=card.product,
                status=card.status,
                limit=card.limit,
                available=card.available,
                expiry=card.expiry,
                last_used_at=None
                if card.used_days_ago is None
                else now - timedelta(days=card.used_days_ago),
                pan=pans[card.card_id],
            )
            e = card.enrollment
            if e is not None and e.token_ref:
                await s.execute(
                    wallet_tokens.insert().values(
                        token_ref=e.token_ref,
                        card_id=card.card_id,
                        enrollment_id=e.enrollment_id,
                        status=e.status,
                        version=e.version,
                        updated_at=now,
                    )
                )
    for relay in runtime.relays:
        relay.wake()


# ---- Test support (TEST_MODE only) ----------------------------------------------------

test_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


class TestCardRequest(BaseModel):
    customer_id: uuid.UUID
    card_id: uuid.UUID | None = Field(None, description="Generated when omitted")
    product: Literal["CREDIT", "DEBIT", "PREPAID"] = "CREDIT"
    status: Literal["ACTIVE", "LOST", "CLOSED"] = "ACTIVE"
    limit: int = Field(300_000, ge=0, description="Cents")
    available: int | None = Field(None, description="Cents; defaults to limit")
    expiry: str | None = Field(None, pattern=r"^\d{4}-\d{2}$", description="Default: 4 years out")
    used_days_ago: int | None = Field(None, ge=0, description="null means never used")


@test_router.post(
    "/cards",
    summary="Create a card (test support)",
    description="Creates a card for any customer and publishes CARD_ACTIVATED. Used by the "
    "POST /test/customers orchestrator.",
    status_code=201,
    response_model=domain.CardView,
)
async def create_test_card(body: TestCardRequest, request: Request) -> domain.CardView:
    db = request.app.state.runtime.db
    now = clock.now()
    card_id = body.card_id or uuid.uuid4()
    async with db.transaction() as s:
        await domain.create_card(
            s,
            card_id=card_id,
            customer_id=body.customer_id,
            product=body.product,
            status=body.status,
            limit=body.limit,
            available=body.limit if body.available is None else body.available,
            expiry=body.expiry or domain.new_expiry(now),
            last_used_at=None
            if body.used_days_ago is None
            else now - timedelta(days=body.used_days_ago),
            pan=domain.new_pan(),
        )
        row = (await s.execute(sa.select(cards).where(cards.c.card_id == card_id))).one()
    for relay in request.app.state.runtime.relays:
        relay.wake()
    return domain.card_view(row)


class TestTokenRequest(BaseModel):
    token_ref: str = Field(min_length=1, max_length=64)
    card_id: uuid.UUID
    enrollment_id: uuid.UUID
    status: Literal["PENDING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]
    version: int = Field(1, ge=1)


@test_router.post(
    "/wallet-tokens",
    summary="Set a token in the local enrolment copy (test support)",
    description="Applies the same rule as an enrollment.changed event (versions only move "
    "forward). Lets tests exercise authorization without the enrolment flow.",
)
async def set_test_token(body: TestTokenRequest, request: Request) -> dict:
    payload = events.EnrollmentChangedPayload(
        enrollment_id=body.enrollment_id,
        card_id=body.card_id,
        customer_id=uuid.UUID(int=0),
        token_ref=body.token_ref,
        status=body.status,
        version=body.version,
        operator_version=None,
    )
    async with request.app.state.runtime.db.transaction() as s:
        await apply_enrollment(s, payload)
        row = (
            await s.execute(
                sa.select(wallet_tokens).where(wallet_tokens.c.token_ref == body.token_ref)
            )
        ).one()
    return {
        "token_ref": row.token_ref,
        "card_id": str(row.card_id),
        "status": row.status,
        "version": row.version,
    }


app = create_app(
    ServiceSpec(
        name="card",
        title="CardVault card-service",
        description=(
            "Card master for CardVault Bank: the customer's cards and their lifecycle, plus the "
            "authorization decision, holds and clearing for purchases arriving from the card "
            "network. The full card number never leaves this service."
        ),
        routers=[customer_router, internal_router],
        test_routers=[test_router],
        uses_db=True,
        uses_kafka=True,
        reset_tables=RESET_TABLES,
        on_startup=[startup],
        on_seed=[seed],
    )
)
