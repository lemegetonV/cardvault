"""Folding auth.decided and txn.cleared into one wallet transaction per trace_id.

Both topics are keyed by card_id, but they are different topics, so a clearing record can
arrive before the authorization it belongs to (spec 6.4). Each event upserts the row by
``trace_id`` and then recomputes ``status`` from the amounts (spec 16.1 rule 6), so the end
state is the same whatever order the events arrive in.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from services.wallet_txn.models import wallet_transactions
from shared import events

DISPUTABLE = ("AUTHORIZED", "SETTLED", "PARTIALLY_REVERSED", "PARTIALLY_REFUNDED")


def derive_status(
    *,
    decision: str | None,
    auth_amount: int | None,
    cleared: int,
    reversed_: int,
    refunded: int,
    settled: bool,
) -> str:
    """Spec 16.1 rule 6. ``decision`` is None while only clearing records have arrived."""
    if decision == "DECLINED":
        return "DECLINED"
    if not settled and reversed_ > 0:
        if auth_amount is not None and reversed_ >= auth_amount:
            return "REVERSED"
        return "PARTIALLY_REVERSED"
    if refunded > 0 and refunded == cleared:
        return "REFUNDED"
    if 0 < refunded < cleared:
        return "PARTIALLY_REFUNDED"
    if settled:
        return "SETTLED"
    return "AUTHORIZED"


def status_of(row) -> str:
    return derive_status(
        decision=row.decision,
        auth_amount=row.auth_amount,
        cleared=row.cleared_amount,
        reversed_=row.reversed_amount,
        refunded=row.refunded_amount,
        settled=row.settled_at is not None,
    )


async def _lock_or_create(
    session,
    *,
    trace_id: str,
    card_id: uuid.UUID,
    customer_id: uuid.UUID,
    token_ref: str | None,
    at: datetime,
    now: datetime,
):
    """The row for ``trace_id``, created as a skeleton if this is the first event for it."""
    await session.execute(
        insert(wallet_transactions)
        .values(
            txn_id=uuid.uuid4(),
            trace_id=trace_id,
            card_id=card_id,
            customer_id=customer_id,
            token_ref=token_ref,
            status="AUTHORIZED",
            occurred_at=at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[wallet_transactions.c.trace_id])
    )
    return (
        await session.execute(
            sa.select(wallet_transactions)
            .where(wallet_transactions.c.trace_id == trace_id)
            .with_for_update()
        )
    ).one()


async def _save(session, trace_id: str, values: dict, now: datetime) -> None:
    await session.execute(
        wallet_transactions.update()
        .where(wallet_transactions.c.trace_id == trace_id)
        .values(**values, updated_at=now)
    )
    row = (
        await session.execute(
            sa.select(wallet_transactions).where(wallet_transactions.c.trace_id == trace_id)
        )
    ).one()
    await session.execute(
        wallet_transactions.update()
        .where(wallet_transactions.c.trace_id == trace_id)
        .values(status=status_of(row))
    )


async def apply_auth(session, p: events.AuthDecidedPayload, now: datetime) -> bool:
    """Returns False when the event is not a wallet purchase (ignored)."""
    if p.token_requestor != "WALLET":
        return False
    row = await _lock_or_create(
        session,
        trace_id=p.trace_id,
        card_id=p.card_id,
        customer_id=p.customer_id,
        token_ref=p.token_ref,
        at=p.decided_at,
        now=now,
    )
    await _save(
        session,
        p.trace_id,
        {
            "token_ref": p.token_ref or row.token_ref,
            "merchant_name": p.merchant_name,
            "mcc": p.mcc,
            "decision": p.decision,
            "decline_code": p.decline_code,
            "auth_amount": p.amount,
            "currency": p.currency,
            "occurred_at": p.decided_at,  # the purchase time, even if clearing came first
        },
        now,
    )
    return True


async def apply_clearing(session, p: events.TxnClearedPayload, now: datetime) -> bool:
    if p.token_requestor != "WALLET":
        return False
    row = await _lock_or_create(
        session,
        trace_id=p.trace_id,
        card_id=p.card_id,
        customer_id=p.customer_id,
        token_ref=p.token_ref,
        at=p.cleared_at,
        now=now,
    )
    values: dict = {}
    if p.type == "SETTLE":
        values = {"cleared_amount": p.amount, "settled_at": p.cleared_at}
    elif p.type == "REVERSE":
        values = {"reversed_amount": row.reversed_amount + p.amount}
    else:
        values = {"refunded_amount": row.refunded_amount + p.amount}
    await _save(session, p.trace_id, values, now)
    return True
