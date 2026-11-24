"""Event envelope, topic names and payload models (spec section 8 and 16.5 rule 28).

Every event is ``{event_id, event_type, version, occurred_at, correlation_id, payload}``.
``event_type`` is the topic name. JSON Schemas generated from these models live in
``docs/events/`` (``python -m shared.events.export``); CI fails if they drift.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from shared import clock, correlation

ENVELOPE_VERSION = 1

# Topics. All keyed by card_id except contact.updated (customer_id).
CARD_LIFECYCLE = "card.lifecycle"
CONTACT_UPDATED = "contact.updated"
ENROLLMENT_CHANGED = "enrollment.changed"
AUTH_DECIDED = "auth.decided"
TXN_CLEARED = "txn.cleared"
ENROLLMENT_DLQ = "enrollment.dlq"
WALLET_TXN_DLQ = "wallet.txn.dlq"

TOPICS = (CARD_LIFECYCLE, CONTACT_UPDATED, ENROLLMENT_CHANGED, AUTH_DECIDED, TXN_CLEARED)
DLQ_TOPICS = (ENROLLMENT_DLQ, WALLET_TXN_DLQ)
ALL_TOPICS = TOPICS + DLQ_TOPICS
PARTITIONS = 3

Cents = Annotated[int, Field(description="Amount in integer minor units (cents)")]
Product = Literal["CREDIT", "DEBIT", "PREPAID"]
CardStatus = Literal["ACTIVE", "LOST", "CLOSED"]
TokenRequestor = Literal["WALLET", "CARD"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardLifecyclePayload(_Payload):
    card_id: uuid.UUID
    customer_id: uuid.UUID
    event: Literal["CARD_ACTIVATED", "CARD_LOST", "CARD_REISSUED", "CARD_CLOSED", "CARD_USED"]
    status: CardStatus
    product: Product
    last4: str = Field(pattern=r"^\d{4}$")
    expiry: str = Field(pattern=r"^\d{4}-\d{2}$", description="Expiry month, YYYY-MM")
    last_used_at: datetime | None


class ContactUpdatedPayload(_Payload):
    customer_id: uuid.UUID
    email: str | None
    email_verified: bool
    phone: str | None = Field(description="E.164")
    phone_verified: bool
    version: int


class EnrollmentChangedPayload(_Payload):
    enrollment_id: uuid.UUID
    card_id: uuid.UUID
    customer_id: uuid.UUID
    token_ref: str | None
    status: Literal["PENDING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]
    version: int
    operator_version: int | None


class AuthDecidedPayload(_Payload):
    trace_id: str
    card_id: uuid.UUID
    customer_id: uuid.UUID
    token_ref: str | None
    token_requestor: TokenRequestor
    decision: Literal["APPROVED", "DECLINED"]
    decline_code: str | None
    amount: Cents
    currency: Literal["USD"]
    merchant_name: str
    mcc: str
    decided_at: datetime


class TxnClearedPayload(_Payload):
    clearing_id: str
    trace_id: str
    type: Literal["SETTLE", "REVERSE", "REFUND"]
    amount: Cents
    card_id: uuid.UUID
    customer_id: uuid.UUID
    token_ref: str | None
    token_requestor: TokenRequestor
    currency: Literal["USD"]
    cleared_at: datetime


PAYLOADS: dict[str, type[BaseModel]] = {
    CARD_LIFECYCLE: CardLifecyclePayload,
    CONTACT_UPDATED: ContactUpdatedPayload,
    ENROLLMENT_CHANGED: EnrollmentChangedPayload,
    AUTH_DECIDED: AuthDecidedPayload,
    TXN_CLEARED: TxnClearedPayload,
}

P = TypeVar("P", bound=BaseModel)


class Envelope(BaseModel, Generic[P]):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: str
    version: Literal[1] = ENVELOPE_VERSION
    occurred_at: datetime
    correlation_id: str | None
    payload: P


class RawEnvelope(BaseModel):
    """Envelope with an unparsed payload: the first validation step for consumers."""

    event_id: uuid.UUID
    event_type: str
    version: int
    occurred_at: datetime
    correlation_id: str | None
    payload: dict[str, Any]


class MalformedEvent(ValueError):
    """Schema validation failed or the envelope version is unknown (rule 30)."""


def make(event_type: str, payload: BaseModel) -> Envelope:
    """Build an envelope stamped with the shared clock and the current correlation ID."""
    expected = PAYLOADS.get(event_type)
    if expected is not None and not isinstance(payload, expected):
        raise TypeError(f"{event_type} needs a {expected.__name__} payload")
    return Envelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=clock.now(),
        correlation_id=correlation.get_or_new(),
        payload=payload,
    )


def parse(raw: bytes | str | dict) -> Envelope:
    """Validate an incoming event. Raises ``MalformedEvent`` on any schema problem."""
    try:
        env = (
            RawEnvelope.model_validate(raw)
            if isinstance(raw, dict)
            else RawEnvelope.model_validate_json(raw)
        )
    except ValueError as exc:
        raise MalformedEvent(str(exc)) from exc
    if env.version != ENVELOPE_VERSION:
        raise MalformedEvent(f"unknown envelope version {env.version}")
    model = PAYLOADS.get(env.event_type)
    if model is None:
        raise MalformedEvent(f"unknown event_type {env.event_type}")
    try:
        payload = model.model_validate(env.payload)
    except ValueError as exc:
        raise MalformedEvent(str(exc)) from exc
    return Envelope(
        event_id=env.event_id,
        event_type=env.event_type,
        occurred_at=env.occurred_at,
        correlation_id=env.correlation_id,
        payload=payload,
    )


def expiry_str(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"
