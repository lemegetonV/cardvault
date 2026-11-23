"""card-service internal API: authorization and clearing, called by network-mock.

Both endpoints need an ``X-Service-Key`` (network-mock or tests) and reject customer JWTs.

Authorization is idempotent by ``trace_id``: the decision is stored with the request, and a
duplicate returns the stored decision without a second hold. Clearing is idempotent by
``clearing_id``; the same ID with a different body returns 422.

Locking order is always card row, then authorization row, so authorization and clearing
for the same card serialise and cannot deadlock.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.dialects.postgresql import insert

from services.card import domain
from services.card.models import (
    authorizations,
    cards,
    holds,
    outbox,
    processed_clearing,
    wallet_tokens,
)
from shared import clock, events
from shared.errors import AppError, ErrorBody
from shared.outbox import enqueue_event
from shared.security import require_service

router = APIRouter(prefix="/internal/v1", tags=["internal"])
service_auth = require_service("network-mock", "tests")

ERRORS = {
    401: {"model": ErrorBody, "description": "Missing or wrong X-Service-Key"},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


class AuthorizationRequest(BaseModel):
    trace_id: str = Field(min_length=1, max_length=64, description="Network trace ID; unique")
    card_id: uuid.UUID = Field(description="The card being charged")
    token_ref: str | None = Field(None, max_length=64, description="Wallet token (WALLET only)")
    token_requestor: Literal["WALLET", "CARD"] = Field(
        description="WALLET for wallet-token purchases, CARD for plain card purchases"
    )
    amount: int = Field(gt=0, description="Amount in cents, > 0")
    currency: Literal["USD"] = Field(description="Only USD is supported")
    merchant_name: str = Field(min_length=1, max_length=200, description="Untrusted free text")
    mcc: str = Field(pattern=r"^\d{4}$", description="Merchant category code, 4 digits")

    @model_validator(mode="after")
    def _token_needed(self) -> AuthorizationRequest:
        if self.token_requestor == "WALLET" and not self.token_ref:
            raise ValueError("token_ref is required when token_requestor is WALLET")
        return self


class AuthorizationResponse(BaseModel):
    trace_id: str
    card_id: uuid.UUID
    decision: Literal["APPROVED", "DECLINED"]
    decline_code: str | None = Field(
        description="CARD_LOST, CARD_CLOSED, CARD_EXPIRED, TOKEN_MISMATCH, TOKEN_INACTIVE "
        "or INSUFFICIENT_FUNDS"
    )
    amount: int
    currency: Literal["USD"] = "USD"
    hold_id: uuid.UUID | None
    decided_at: datetime


class _Duplicate(Exception):
    """Raised inside the transaction to roll it back when another request won the trace_id."""


def _relay(request: Request) -> None:
    for relay in request.app.state.runtime.relays:
        relay.wake()


async def _stored_decision(session, trace_id: str) -> dict | None:
    return await session.scalar(
        sa.select(authorizations.c.response).where(authorizations.c.trace_id == trace_id)
    )


async def _decide(session, card, body: AuthorizationRequest) -> str | None:
    """The decision order from the spec; returns a decline code or None to approve."""
    if card.status == "LOST":
        return "CARD_LOST"
    if card.status == "CLOSED":
        return "CARD_CLOSED"
    if domain.is_expired(card.expiry):
        return "CARD_EXPIRED"
    if body.token_requestor == "WALLET":
        token = (
            await session.execute(
                sa.select(wallet_tokens).where(wallet_tokens.c.token_ref == body.token_ref)
            )
        ).first()
        if token is None or token.card_id != card.card_id:
            return "TOKEN_MISMATCH"
        if token.status != "ENROLLED":
            return "TOKEN_INACTIVE"
    if body.amount > card.available_amount:
        return "INSUFFICIENT_FUNDS"
    return None


@router.post(
    "/authorizations",
    summary="Decide an authorization",
    description=(
        "Called by the card network for every purchase. Checks, in order: card LOST, card "
        "CLOSED, card expired, token not mapped to the card (WALLET), token not ENROLLED in the "
        "local copy of enrolment status (WALLET), amount above available. Approval creates a "
        "hold and reduces available. Both outcomes publish auth.decided. A repeated trace_id "
        "returns the original decision and creates no second hold."
    ),
    response_model=AuthorizationResponse,
    responses=ERRORS,
)
async def authorize(
    body: AuthorizationRequest, request: Request, _: str = Depends(service_auth)
) -> AuthorizationResponse:
    db = request.app.state.runtime.db
    async with db.session() as s:
        stored = await _stored_decision(s, body.trace_id)
    if stored is not None:
        return AuthorizationResponse.model_validate(stored)

    try:
        async with db.transaction() as s:
            card = (
                await s.execute(
                    sa.select(cards).where(cards.c.card_id == body.card_id).with_for_update()
                )
            ).first()
            if card is None:
                raise AppError(404, "CARD_NOT_FOUND", "Card not found")
            # Another request with this trace_id may have finished while we waited.
            stored = await _stored_decision(s, body.trace_id)
            if stored is not None:
                return AuthorizationResponse.model_validate(stored)

            now = clock.now()
            decline_code = await _decide(s, card, body)
            hold_id = None
            if decline_code is None:
                hold_id = uuid.uuid4()
                await s.execute(
                    holds.insert().values(
                        hold_id=hold_id,
                        card_id=card.card_id,
                        trace_id=body.trace_id,
                        original_amount=body.amount,
                        amount=body.amount,
                        status="ACTIVE",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await s.execute(
                    cards.update()
                    .where(cards.c.card_id == card.card_id)
                    .values(
                        available_amount=cards.c.available_amount - body.amount,
                        last_used_at=now,
                        updated_at=now,
                    )
                )
            response = AuthorizationResponse(
                trace_id=body.trace_id,
                card_id=card.card_id,
                decision="DECLINED" if decline_code else "APPROVED",
                decline_code=decline_code,
                amount=body.amount,
                hold_id=hold_id,
                decided_at=now,
            )
            inserted = await s.execute(
                insert(authorizations)
                .values(
                    trace_id=body.trace_id,
                    card_id=card.card_id,
                    customer_id=card.customer_id,
                    token_ref=body.token_ref,
                    token_requestor=body.token_requestor,
                    amount=body.amount,
                    currency=body.currency,
                    merchant_name=body.merchant_name,
                    mcc=body.mcc,
                    decision=response.decision,
                    decline_code=decline_code,
                    hold_id=hold_id,
                    decided_at=now,
                    response=response.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
                .returning(authorizations.c.trace_id)
            )
            if inserted.first() is None:
                raise _Duplicate  # same trace_id on another card won the race; roll back

            await enqueue_event(
                s,
                outbox,
                events.AUTH_DECIDED,
                str(card.card_id),
                events.make(
                    events.AUTH_DECIDED,
                    events.AuthDecidedPayload(
                        trace_id=body.trace_id,
                        card_id=card.card_id,
                        customer_id=card.customer_id,
                        token_ref=body.token_ref,
                        token_requestor=body.token_requestor,
                        decision=response.decision,
                        decline_code=decline_code,
                        amount=body.amount,
                        currency=body.currency,
                        merchant_name=body.merchant_name,
                        mcc=body.mcc,
                        decided_at=now,
                    ),
                ),
            )
            if decline_code is None:
                used = (
                    await s.execute(sa.select(cards).where(cards.c.card_id == card.card_id))
                ).one()
                await domain.lifecycle_event(s, used, "CARD_USED")
    except _Duplicate:
        async with db.session() as s:
            return AuthorizationResponse.model_validate(await _stored_decision(s, body.trace_id))
    _relay(request)
    return response


class ClearingRequest(BaseModel):
    clearing_id: str = Field(min_length=1, max_length=64, description="Unique per clearing record")
    trace_id: str = Field(min_length=1, max_length=64, description="The authorization's trace_id")
    type: Literal["SETTLE", "REVERSE", "REFUND"]
    amount: int = Field(gt=0, description="Amount in cents, > 0")
    currency: Literal["USD"] = "USD"


class ClearingResponse(BaseModel):
    clearing_id: str
    trace_id: str
    type: Literal["SETTLE", "REVERSE", "REFUND"]
    amount: int
    card_id: uuid.UUID
    hold_status: Literal["ACTIVE", "SETTLED", "RELEASED"]
    hold_amount: int = Field(description="Amount still held, in cents")
    cleared_amount: int
    reversed_amount: int
    refunded_amount: int
    available: int = Field(description="Card available after this record, in cents")
    cleared_at: datetime


def _fingerprint(body: ClearingRequest) -> str:
    return hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


@router.post(
    "/clearing",
    summary="Apply a clearing record",
    description=(
        "SETTLE settles the hold at the cleared amount (it may differ from the authorization; "
        "only CREDIT may go above the hold). REVERSE releases all or part of an unsettled hold. "
        "REFUND credits up to cleared minus already refunded; DEBIT and PREPAID never go above "
        "their limit. Idempotent by clearing_id. Publishes txn.cleared."
    ),
    response_model=ClearingResponse,
    responses=ERRORS,
)
async def clearing(
    body: ClearingRequest, request: Request, _: str = Depends(service_auth)
) -> ClearingResponse:
    db = request.app.state.runtime.db
    fingerprint = _fingerprint(body)
    async with db.transaction() as s:
        auth = (
            await s.execute(
                sa.select(authorizations.c.card_id).where(
                    authorizations.c.trace_id == body.trace_id
                )
            )
        ).first()
        prior = await _prior_clearing(s, body.clearing_id, fingerprint)
        if prior is not None:
            return prior
        if auth is None:
            raise AppError(404, "TRACE_NOT_FOUND", "No authorization with this trace_id")
        # Lock order: card, then authorization, then hold.
        card = (
            await s.execute(
                sa.select(cards).where(cards.c.card_id == auth.card_id).with_for_update()
            )
        ).one()
        prior = await _prior_clearing(s, body.clearing_id, fingerprint)
        if prior is not None:
            return prior
        a = (
            await s.execute(
                sa.select(authorizations)
                .where(authorizations.c.trace_id == body.trace_id)
                .with_for_update()
            )
        ).one()
        if a.decision != "APPROVED":
            raise AppError(409, "AUTH_DECLINED", "The authorization was declined")
        hold = (
            await s.execute(
                sa.select(holds).where(holds.c.trace_id == body.trace_id).with_for_update()
            )
        ).one()

        now = clock.now()
        available = card.available_amount
        hold_amount, hold_status = hold.amount, hold.status
        cleared, reversed_, refunded = a.cleared_amount, a.reversed_amount, a.refunded_amount
        settled_at = a.settled_at

        if body.type == "SETTLE":
            if settled_at is not None:
                raise AppError(409, "ALREADY_SETTLED", "This authorization is already settled")
            if hold_status == "RELEASED":
                raise AppError(409, "HOLD_RELEASED", "The hold was fully reversed")
            if body.amount > hold_amount and card.product != "CREDIT":
                raise AppError(
                    422,
                    "OVER_SETTLE_NOT_ALLOWED",
                    "Only CREDIT cards may settle above the held amount",
                    {"hold_amount": hold_amount, "amount": body.amount},
                )
            # Release what is held, charge what cleared.
            available = available + hold_amount - body.amount
            hold_status, cleared, settled_at = "SETTLED", body.amount, now
        elif body.type == "REVERSE":
            if settled_at is not None:
                raise AppError(409, "ALREADY_SETTLED", "Settled transactions are refunded")
            if hold_status == "RELEASED":
                raise AppError(409, "HOLD_RELEASED", "The hold was already fully reversed")
            if body.amount > hold_amount:
                raise AppError(
                    422,
                    "AMOUNT_EXCEEDS_HOLD",
                    "Reversal is larger than the amount held",
                    {"hold_amount": hold_amount, "amount": body.amount},
                )
            hold_amount -= body.amount
            available += body.amount
            reversed_ += body.amount
            if hold_amount == 0:
                hold_status = "RELEASED"
        else:  # REFUND
            if settled_at is None:
                raise AppError(409, "NOT_SETTLED", "Only a settled transaction can be refunded")
            refundable = cleared - refunded
            if body.amount > refundable:
                raise AppError(
                    422,
                    "REFUND_EXCEEDS_CLEARED",
                    "Refund is larger than the amount left to refund",
                    {"refundable": refundable, "amount": body.amount},
                )
            refunded += body.amount
            available += body.amount
            if card.product != "CREDIT":
                available = min(available, card.limit_amount)

        await s.execute(
            cards.update()
            .where(cards.c.card_id == card.card_id)
            .values(available_amount=available, updated_at=now)
        )
        await s.execute(
            holds.update()
            .where(holds.c.hold_id == hold.hold_id)
            .values(amount=hold_amount, status=hold_status, updated_at=now)
        )
        await s.execute(
            authorizations.update()
            .where(authorizations.c.trace_id == body.trace_id)
            .values(
                cleared_amount=cleared,
                reversed_amount=reversed_,
                refunded_amount=refunded,
                settled_at=settled_at,
            )
        )
        response = ClearingResponse(
            clearing_id=body.clearing_id,
            trace_id=body.trace_id,
            type=body.type,
            amount=body.amount,
            card_id=card.card_id,
            hold_status=hold_status,
            hold_amount=hold_amount,
            cleared_amount=cleared,
            reversed_amount=reversed_,
            refunded_amount=refunded,
            available=available,
            cleared_at=now,
        )
        await s.execute(
            processed_clearing.insert().values(
                clearing_id=body.clearing_id,
                trace_id=body.trace_id,
                type=body.type,
                amount=body.amount,
                fingerprint=fingerprint,
                response=response.model_dump(mode="json"),
                created_at=now,
            )
        )
        await enqueue_event(
            s,
            outbox,
            events.TXN_CLEARED,
            str(card.card_id),
            events.make(
                events.TXN_CLEARED,
                events.TxnClearedPayload(
                    clearing_id=body.clearing_id,
                    trace_id=body.trace_id,
                    type=body.type,
                    amount=body.amount,
                    card_id=card.card_id,
                    customer_id=card.customer_id,
                    token_ref=a.token_ref,
                    token_requestor=a.token_requestor,
                    currency=body.currency,
                    cleared_at=now,
                ),
            ),
        )
    _relay(request)
    return response


async def _prior_clearing(session, clearing_id: str, fingerprint: str) -> ClearingResponse | None:
    prior = (
        await session.execute(
            sa.select(processed_clearing).where(processed_clearing.c.clearing_id == clearing_id)
        )
    ).first()
    if prior is None:
        return None
    if prior.fingerprint != fingerprint:
        raise AppError(
            422, "CLEARING_ID_REUSED", "This clearing_id was used for a different record"
        )
    return ClearingResponse.model_validate(prior.response)
