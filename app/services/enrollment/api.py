"""enrollment-service customer API: the wallet as the customer sees it.

* ``GET /v1/wallet``: every card with its eligibility (and the first failing reason) and
  its enrolment status.
* ``POST /v1/wallet/opt-out``: every active enrolment becomes REMOVED at once.
* ``POST /v1/wallet/opt-in {terms_version}``: clears the opt-out and per-card removals and
  enrols every eligible card.
* ``POST /v1/wallet/cards/{card_id} {terms_version}``: add one eligible card.
* ``DELETE /v1/wallet/cards/{card_id}``: remove one card; the sweep never re-adds it.

Destructive actions are idempotent: repeating one returns 200 with the current state and
publishes nothing (spec 16.1 rule 2). Every POST and DELETE accepts ``Idempotency-Key``.
Another customer's card returns 404, exactly like a card that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from services.enrollment import domain, eligibility
from services.enrollment.models import (
    card_projection,
    enrollments,
    idempotency_keys,
    wallet_card_prefs,
    wallet_prefs,
)
from shared import bugs, clock, idempotency
from shared.errors import AppError, ErrorBody
from shared.security import Customer, require_customer

router = APIRouter(prefix="/v1/wallet", tags=["wallet"])

CARD_ID = Path(description="The card's ID (UUID)")
IDEMPOTENCY_DOC = " Accepts `Idempotency-Key`: the same key returns the same response."
EnrollmentStatus = Literal["PENDING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]


class EnrollmentView(BaseModel):
    enrollment_id: uuid.UUID
    status: EnrollmentStatus
    version: int = Field(description="Increases with every change sent to the wallet operator")
    last4: str = Field(description="last4 the wallet operator has for this card")
    expiry: str
    status_reason: str | None = Field(description="Why the card has this status, if known")
    failure_reason: str | None = Field(description="Why the last operator request failed")
    updated_at: datetime


class WalletCard(BaseModel):
    card_id: uuid.UUID
    product: Literal["CREDIT", "DEBIT", "PREPAID"]
    last4: str
    expiry: str = Field(description="Expiry month, YYYY-MM")
    card_status: Literal["ACTIVE", "LOST", "CLOSED"]
    eligible: bool = Field(description="Whether the card can be added to the wallet now")
    ineligible_reason: str | None = Field(
        description="First failing eligibility rule: " + ", ".join(eligibility.REASONS)
    )
    enrollment: EnrollmentView | None = Field(description="null if never enrolled")


class Wallet(BaseModel):
    customer_id: uuid.UUID
    opted_out: bool
    terms_version: str | None = Field(description="Terms version the customer last accepted")
    cards: list[WalletCard]


class TermsBody(BaseModel):
    terms_version: str = Field(
        min_length=1, max_length=32, description="Version of the wallet terms the customer accepted"
    )


ERRORS = {401: {"model": ErrorBody}, 404: {"model": ErrorBody, "description": "No such card"}}


def _db(request: Request):
    return request.app.state.runtime.db


def _wake(request: Request) -> None:
    for relay in request.app.state.runtime.relays:
        relay.wake()


def enrollment_view(enr) -> EnrollmentView | None:
    if enr is None:
        return None
    return EnrollmentView(
        enrollment_id=enr.enrollment_id,
        status=enr.status,
        version=enr.version,
        last4=enr.last4,
        expiry=enr.expiry,
        status_reason=enr.status_reason,
        failure_reason=enr.failure_reason,
        updated_at=enr.updated_at,
    )


async def card_view(s, card) -> WalletCard:
    enr = await domain.get_enrollment(s, card_id=card.card_id)
    result = await domain.check(s, card)
    return WalletCard(
        card_id=card.card_id,
        product=card.product,
        last4=card.last4,
        expiry=card.expiry,
        card_status=card.status,
        eligible=result.eligible,
        ineligible_reason=result.reason,
        enrollment=enrollment_view(enr),
    )


async def wallet_view(s, customer_id: uuid.UUID) -> Wallet:
    prefs = await domain.get_prefs(s, customer_id)
    cards = (
        await s.execute(
            sa.select(card_projection)
            .where(card_projection.c.customer_id == customer_id)
            .order_by(card_projection.c.card_id)
        )
    ).all()
    return Wallet(
        customer_id=customer_id,
        opted_out=bool(prefs and prefs.opted_out),
        terms_version=prefs.terms_version if prefs else None,
        cards=[await card_view(s, c) for c in cards],
    )


async def _own_card(s, card_id: uuid.UUID, customer: Customer):
    card = await domain.get_card(s, card_id)
    if card is None or card.customer_id != customer.customer_id:
        raise AppError(404, "CARD_NOT_FOUND", "Card not found")
    return card


async def _set_prefs(s, customer_id, **values) -> None:
    now = clock.now()
    stmt = insert(wallet_prefs).values(
        **{"opted_out": False, **values}, customer_id=customer_id, updated_at=now
    )
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=[wallet_prefs.c.customer_id], set_={**values, "updated_at": now}
        )
    )


async def _lock_customer(s, customer_id) -> None:
    """Serialise wallet-wide actions of one customer (opt-out against add, and so on)."""
    await s.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(str(customer_id), 0)))
    )


@router.get(
    "",
    summary="View my wallet",
    description="Every card of the customer with its wallet eligibility (and the first failing "
    "reason) and its enrolment status.",
    response_model=Wallet,
    responses={401: {"model": ErrorBody}},
)
async def get_wallet(request: Request, customer: Customer = Depends(require_customer)) -> Wallet:
    async with _db(request).session() as s:
        return await wallet_view(s, customer.customer_id)


@router.get(
    "/cards/{card_id}",
    summary="View one card in my wallet",
    description="Eligibility and enrolment status of one card (useful for polling PENDING).",
    response_model=WalletCard,
    responses=ERRORS,
)
async def get_wallet_card(
    request: Request, card_id: uuid.UUID = CARD_ID, customer: Customer = Depends(require_customer)
) -> WalletCard:
    async with _db(request).session() as s:
        return await card_view(s, await _own_card(s, card_id, customer))


@router.post(
    "/opt-out",
    summary="Opt out of the wallet",
    description="Removes every card from the wallet at once (status REMOVED) and tells the "
    "wallet operator. Repeating it returns 200 with the current wallet." + IDEMPOTENCY_DOC,
    response_model=Wallet,
    responses={401: {"model": ErrorBody}},
)
async def opt_out(request: Request, customer: Customer = Depends(require_customer)):
    async def handler():
        async with _db(request).transaction() as s:
            await _lock_customer(s, customer.customer_id)
            prefs = await domain.get_prefs(s, customer.customer_id)
            if not (prefs and prefs.opted_out):
                await _set_prefs(s, customer.customer_id, opted_out=True)
                rows = (
                    await s.execute(
                        sa.select(enrollments)
                        .where(
                            enrollments.c.customer_id == customer.customer_id,
                            enrollments.c.status.in_(domain.ACTIVE),
                        )
                        .with_for_update()
                    )
                ).all()
                if bugs.enabled("BUG_OPT_OUT_PARTIAL") and len(rows) > 1:
                    # Seeded bug: the last card (by card_id) stays enrolled.
                    rows = sorted(rows, key=lambda r: str(r.card_id))[:-1]
                for enr in rows:
                    await domain.remove_now(s, enr, reason="CUSTOMER_OPTED_OUT", source="CUSTOMER")
            wallet = await wallet_view(s, customer.customer_id)
        _wake(request)
        return 200, wallet

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


@router.post(
    "/opt-in",
    summary="Opt in to the wallet",
    description="Accepts the given terms version, clears the opt-out and any per-card "
    "removals, and enrols every eligible card (each becomes PENDING, then ENROLLED when the "
    "wallet operator confirms)." + IDEMPOTENCY_DOC,
    response_model=Wallet,
    responses={401: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def opt_in(body: TermsBody, request: Request, customer: Customer = Depends(require_customer)):
    async def handler():
        async with _db(request).transaction() as s:
            await _lock_customer(s, customer.customer_id)
            await _set_prefs(
                s, customer.customer_id, opted_out=False, terms_version=body.terms_version
            )
            await s.execute(
                wallet_card_prefs.delete().where(
                    wallet_card_prefs.c.customer_id == customer.customer_id
                )
            )
            cards = (
                await s.execute(
                    sa.select(card_projection).where(
                        card_projection.c.customer_id == customer.customer_id
                    )
                )
            ).all()
            for card in cards:
                enr = await domain.get_enrollment(s, card_id=card.card_id)
                if enr is not None and enr.status in domain.ACTIVE:
                    continue
                if (await domain.check(s, card)).eligible:
                    await domain.enrol(s, card, terms_version=body.terms_version, source="CUSTOMER")
            wallet = await wallet_view(s, customer.customer_id)
        _wake(request)
        return 200, wallet

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


@router.post(
    "/cards/{card_id}",
    summary="Add a card to my wallet",
    description="Starts enrolment (202, status PENDING). A card already ENROLLED, PENDING or "
    "SUSPENDED returns 200 with its current state. An opted-out customer gets 409 "
    "WALLET_OPTED_OUT; an ineligible card gets 422 CARD_INELIGIBLE with the reason in "
    "details.reason. Clears an earlier removal of this card." + IDEMPOTENCY_DOC,
    response_model=WalletCard,
    status_code=202,
    responses={
        **ERRORS,
        200: {"model": WalletCard, "description": "Already in the wallet"},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
    },
)
async def add_card(
    body: TermsBody,
    request: Request,
    card_id: uuid.UUID = CARD_ID,
    customer: Customer = Depends(require_customer),
):
    async def handler():
        async with _db(request).transaction() as s:
            await _lock_customer(s, customer.customer_id)
            card = await _own_card(s, card_id, customer)
            prefs = await domain.get_prefs(s, customer.customer_id)
            if prefs and prefs.opted_out:
                raise AppError(409, "WALLET_OPTED_OUT", "Opt in to the wallet before adding a card")
            enr = await domain.get_enrollment(s, card_id=card_id)
            if enr is not None and enr.status in domain.ACTIVE:
                return 200, await card_view(s, card)
            result = await domain.check(s, card, ignore_customer_removal=True)
            if not result.eligible:
                raise AppError(
                    422,
                    "CARD_INELIGIBLE",
                    "This card cannot be added to the wallet",
                    {"reason": result.reason},
                )
            await s.execute(
                wallet_card_prefs.delete().where(wallet_card_prefs.c.card_id == card_id)
            )
            await domain.enrol(s, card, terms_version=body.terms_version, source="CUSTOMER")
            view = await card_view(s, card)
        _wake(request)
        return 202, view

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


@router.delete(
    "/cards/{card_id}",
    summary="Remove a card from my wallet",
    description="The enrolment becomes REMOVED at once and the wallet operator is told. The "
    "eligibility sweep never re-adds this card; adding it again or opting in clears that. "
    "Repeating it returns 200 with the current state." + IDEMPOTENCY_DOC,
    response_model=WalletCard,
    responses=ERRORS,
)
async def remove_card(
    request: Request, card_id: uuid.UUID = CARD_ID, customer: Customer = Depends(require_customer)
):
    async def handler():
        async with _db(request).transaction() as s:
            await _lock_customer(s, customer.customer_id)
            card = await _own_card(s, card_id, customer)
            now = clock.now()
            stmt = insert(wallet_card_prefs).values(
                card_id=card_id, customer_id=customer.customer_id, opted_out=True, updated_at=now
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[wallet_card_prefs.c.card_id],
                    set_={"opted_out": True, "updated_at": now},
                )
            )
            enr = await domain.get_enrollment(s, card_id=card_id, lock=True)
            if enr is not None and enr.status in domain.ACTIVE:
                await domain.remove_now(s, enr, reason="CUSTOMER_REMOVED", source="CUSTOMER")
            view = await card_view(s, card)
        _wake(request)
        return 200, view

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )
