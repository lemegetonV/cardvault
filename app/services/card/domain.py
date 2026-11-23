"""card-service domain helpers shared by the customer, internal and test APIs."""

from __future__ import annotations

import random
import secrets
import uuid
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.card import pan as pan_crypto
from services.card.models import cards, outbox
from shared import clock, events, seed_data
from shared.outbox import enqueue_event

Product = Literal["CREDIT", "DEBIT", "PREPAID"]
CardStatus = Literal["ACTIVE", "LOST", "CLOSED"]


class CardView(BaseModel):
    """A card as the customer sees it. The full card number is never returned."""

    card_id: uuid.UUID
    product: Product
    last4: str = Field(description="Last four digits of the card number")
    expiry: str = Field(description="Expiry month, YYYY-MM")
    status: CardStatus
    limit: int = Field(description="Credit limit or balance ceiling, in cents")
    available: int = Field(description="Available to spend, in cents (may be negative for CREDIT)")
    currency: Literal["USD"] = "USD"
    last_used_at: datetime | None
    created_at: datetime


def card_view(row) -> CardView:
    return CardView(
        card_id=row.card_id,
        product=row.product,
        last4=row.last4,
        expiry=row.expiry,
        status=row.status,
        limit=row.limit_amount,
        available=row.available_amount,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


def expires_at(expiry: str) -> datetime:
    """A card expires at 00:00 UTC on the first day of the month after its expiry month."""
    year, month = (int(p) for p in expiry.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def is_expired(expiry: str, now: datetime | None = None) -> bool:
    return (now or clock.now()) >= expires_at(expiry)


def new_expiry(now: datetime, years: int = 4) -> str:
    return f"{now.year + years:04d}-{now.month:02d}"


def new_pan() -> str:
    return seed_data.make_pan(random.Random(secrets.randbits(64)))


async def lifecycle_event(session: AsyncSession, row, event: str) -> None:
    """Enqueue a card.lifecycle event for the card as it is now (same transaction)."""
    payload = events.CardLifecyclePayload(
        card_id=row.card_id,
        customer_id=row.customer_id,
        event=event,
        status=row.status,
        product=row.product,
        last4=row.last4,
        expiry=row.expiry,
        last_used_at=row.last_used_at,
    )
    await enqueue_event(
        session,
        outbox,
        events.CARD_LIFECYCLE,
        str(row.card_id),
        events.make(events.CARD_LIFECYCLE, payload),
    )


async def create_card(
    session: AsyncSession,
    *,
    card_id: uuid.UUID,
    customer_id: uuid.UUID,
    product: str,
    status: str,
    limit: int,
    available: int,
    expiry: str,
    last_used_at: datetime | None,
    pan: str,
) -> None:
    """Insert a card and publish CARD_ACTIVATED (spec 16.1 rule 3)."""
    now = clock.now()
    await session.execute(
        cards.insert().values(
            card_id=card_id,
            customer_id=customer_id,
            product=product,
            pan_encrypted=pan_crypto.encrypt(pan, card_id),
            last4=pan[-4:],
            expiry=expiry,
            status=status,
            limit_amount=limit,
            available_amount=available,
            last_used_at=last_used_at,
            created_at=now,
            updated_at=now,
        )
    )
    row = (await session.execute(sa.select(cards).where(cards.c.card_id == card_id))).one()
    await lifecycle_event(session, row, "CARD_ACTIVATED")
