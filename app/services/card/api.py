"""card-service customer API: list and view cards, report lost, reissue, close.

Another customer's card returns 404, exactly like a card that does not exist, so card IDs
cannot be probed. Every state change accepts ``Idempotency-Key``. Repeating a destructive
action that already happened (report lost on a LOST card, close a CLOSED card) returns 200
with the current state and publishes nothing (spec 16.1 rules 1 and 2).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.card import domain
from services.card import pan as pan_crypto
from services.card.models import cards, idempotency_keys
from shared import bugs, clock, idempotency, pagination
from shared.errors import AppError, ErrorBody
from shared.security import Customer, require_customer

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/cards", tags=["cards"])

CARD_ID = Path(description="The card's ID (UUID)")
IDEMPOTENCY_DOC = (
    " Send an `Idempotency-Key` header to make retries safe: the same key returns the same "
    "response."
)
ERRORS = {
    401: {"model": ErrorBody},
    404: {"model": ErrorBody, "description": "No such card for this customer"},
    409: {"model": ErrorBody, "description": "Transition not allowed from the current status"},
}


class CardPage(BaseModel):
    items: list[domain.CardView]
    next_cursor: str | None


def _db(request: Request):
    return request.app.state.runtime.db


def _relay(request: Request) -> None:
    for relay in request.app.state.runtime.relays:
        relay.wake()


async def _own_card(session, card_id: uuid.UUID, customer: Customer, lock: bool = False):
    q = sa.select(cards).where(cards.c.card_id == card_id)
    if lock:
        q = q.with_for_update()
    row = (await session.execute(q)).first()
    if row is None or row.customer_id != customer.customer_id:
        raise AppError(404, "CARD_NOT_FOUND", "Card not found")
    return row


@router.get(
    "",
    summary="List my cards",
    description="The customer's cards, newest first, with cursor pagination.",
    response_model=CardPage,
    responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
)
async def list_cards(
    request: Request,
    customer: Customer = Depends(require_customer),
    limit: int = Depends(pagination.limit_param),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
) -> CardPage:
    q = sa.select(cards).where(cards.c.customer_id == customer.customer_id)
    if cursor:
        ts, last_id = pagination.decode(cursor)
        q = q.where(sa.tuple_(cards.c.created_at, cards.c.card_id) < (ts, uuid.UUID(last_id)))
    q = q.order_by(cards.c.created_at.desc(), cards.c.card_id.desc()).limit(limit + 1)
    async with _db(request).session() as s:
        rows = (await s.execute(q)).all()
    page = rows[:limit]
    next_cursor = (
        pagination.encode(page[-1].created_at, page[-1].card_id) if len(rows) > limit else None
    )
    return CardPage(items=[domain.card_view(r) for r in page], next_cursor=next_cursor)


@router.get(
    "/{card_id}",
    summary="Get one of my cards",
    description="Returns the card if it belongs to the caller; otherwise 404.",
    response_model=domain.CardView,
    responses=ERRORS,
)
async def get_card(
    request: Request,
    card_id: uuid.UUID = CARD_ID,
    customer: Customer = Depends(require_customer),
) -> domain.CardView:
    async with _db(request).session() as s:
        return domain.card_view(await _own_card(s, card_id, customer))


async def _transition(request: Request, card_id: uuid.UUID, customer: Customer, action: str):
    async def handler():
        async with _db(request).transaction() as s:
            row = await _own_card(s, card_id, customer, lock=True)
            now = clock.now()
            if action == "report-lost":
                if row.status == "LOST":
                    return 200, domain.card_view(row)
                if row.status != "ACTIVE":
                    raise _invalid(row.status, action)
                await s.execute(
                    cards.update()
                    .where(cards.c.card_id == card_id)
                    .values(status="LOST", updated_at=now)
                )
                events_to_send = ["CARD_LOST"]
            elif action == "close":
                if row.status == "CLOSED":
                    return 200, domain.card_view(row)
                await s.execute(
                    cards.update()
                    .where(cards.c.card_id == card_id)
                    .values(status="CLOSED", updated_at=now)
                )
                events_to_send = ["CARD_CLOSED"]
            else:  # reissue: ACTIVE or LOST -> ACTIVE with a new number and expiry
                if row.status == "CLOSED":
                    raise _invalid(row.status, action)
                pan = domain.new_pan()
                await s.execute(
                    cards.update()
                    .where(cards.c.card_id == card_id)
                    .values(
                        status="ACTIVE",
                        pan_encrypted=pan_crypto.encrypt(pan, card_id),
                        last4=pan[-4:],
                        expiry=domain.new_expiry(now),
                        updated_at=now,
                    )
                )
                events_to_send = ["CARD_ACTIVATED", "CARD_REISSUED"]
                log.info("card reissued", extra={"card_id": str(card_id), "last4": pan[-4:]})
                if bugs.enabled("BUG_PAN_IN_LOGS"):
                    # Seeded bug: the full card number is logged, past the redaction.
                    log.info("reissued card number", extra={"pan": pan, "_skip_redaction": True})
            updated = (await s.execute(sa.select(cards).where(cards.c.card_id == card_id))).one()
            for event in events_to_send:
                await domain.lifecycle_event(s, updated, event)
        _relay(request)
        return 200, domain.card_view(updated)

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


def _invalid(status: str, action: str) -> AppError:
    return AppError(
        409,
        "INVALID_STATE_TRANSITION",
        f"Cannot {action.replace('-', ' ')} a {status} card",
        {"status": status, "action": action},
    )


@router.post(
    "/{card_id}/report-lost",
    summary="Report a card lost",
    description="ACTIVE -> LOST. Repeating it on a LOST card returns 200 with no change. "
    "A CLOSED card returns 409 INVALID_STATE_TRANSITION." + IDEMPOTENCY_DOC,
    response_model=domain.CardView,
    responses=ERRORS,
)
async def report_lost(
    request: Request,
    card_id: uuid.UUID = CARD_ID,
    customer: Customer = Depends(require_customer),
) -> JSONResponse:
    return await _transition(request, card_id, customer, "report-lost")


@router.post(
    "/{card_id}/reissue",
    summary="Reissue a card",
    description="ACTIVE or LOST -> ACTIVE with a new card number, last4 and expiry. The card_id "
    "stays the same, so wallet enrolment survives. A CLOSED card returns 409." + IDEMPOTENCY_DOC,
    response_model=domain.CardView,
    responses=ERRORS,
)
async def reissue(
    request: Request,
    card_id: uuid.UUID = CARD_ID,
    customer: Customer = Depends(require_customer),
) -> JSONResponse:
    return await _transition(request, card_id, customer, "reissue")


@router.post(
    "/{card_id}/close",
    summary="Close a card",
    description="ACTIVE or LOST -> CLOSED, permanently. Repeating it on a CLOSED card returns "
    "200 with no change." + IDEMPOTENCY_DOC,
    response_model=domain.CardView,
    responses=ERRORS,
)
async def close(
    request: Request,
    card_id: uuid.UUID = CARD_ID,
    customer: Customer = Depends(require_customer),
) -> JSONResponse:
    return await _transition(request, card_id, customer, "close")
