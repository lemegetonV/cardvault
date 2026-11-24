"""wallet-txn-service application: the auth.decided / txn.cleared consumer, seed and test APIs."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.dialects.postgresql import insert

from services.wallet_txn import domain
from services.wallet_txn.api import router as txn_router
from services.wallet_txn.disputes import router as dispute_router
from services.wallet_txn.models import (
    RESET_TABLES,
    disputes,
    processed_events,
    wallet_transactions,
)
from shared import clock, events, seed_txns
from shared.errors import AppError
from shared.kafka import Consumer
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_test_key
from shared.settings import get_settings

CONSUMER_GROUP = "wallet-txn-service"


async def handle_event(runtime: Runtime, envelope: events.Envelope) -> None:
    """Deduplicated by event_id in the same transaction as the change (spec 6.4)."""
    now = clock.now()
    async with runtime.db.transaction() as s:
        first = (
            await s.execute(
                insert(processed_events)
                .values(event_id=envelope.event_id, topic=envelope.event_type, processed_at=now)
                .on_conflict_do_nothing()
                .returning(processed_events.c.event_id)
            )
        ).first()
        if first is None:
            return
        if envelope.event_type == events.AUTH_DECIDED:
            await domain.apply_auth(s, envelope.payload, now)
        elif envelope.event_type == events.TXN_CLEARED:
            await domain.apply_clearing(s, envelope.payload, now)


async def startup(runtime: Runtime) -> None:
    s = get_settings()

    async def handler(envelope: events.Envelope) -> None:
        await handle_event(runtime, envelope)

    runtime.consumers.append(
        Consumer(
            bootstrap=s.kafka_bootstrap,
            group_id=CONSUMER_GROUP,
            topics=[events.AUTH_DECIDED, events.TXN_CLEARED],
            handler=handler,
            dlq_topic=events.WALLET_TXN_DLQ,
            producer=runtime.producer,
            backoff=s.event_backoff,
        )
    )


async def seed(runtime: Runtime) -> None:
    """The seed transactions and the one open dispute, written directly (rule 47)."""
    now = clock.now()
    async with runtime.db.transaction() as s:
        for t in seed_txns.TXNS:
            at = t.occurred_at(now)
            await s.execute(
                wallet_transactions.insert().values(
                    txn_id=t.txn_id,
                    trace_id=t.trace_id,
                    card_id=t.card.card_id,
                    customer_id=t.card.customer_id,
                    token_ref=t.token_ref,
                    merchant_name=t.merchant_name,
                    mcc=t.mcc,
                    decision=t.decision,
                    decline_code=t.decline_code,
                    auth_amount=t.amount,
                    cleared_amount=t.cleared,
                    reversed_amount=t.reversed,
                    refunded_amount=t.refunded,
                    status=domain.derive_status(
                        decision=t.decision,
                        auth_amount=t.amount,
                        cleared=t.cleared,
                        reversed_=t.reversed,
                        refunded=t.refunded,
                        settled=t.settled,
                    ),
                    occurred_at=at,
                    settled_at=t.settled_at(now),
                    created_at=at,
                    updated_at=now,
                )
            )
            if t.disputed:
                await s.execute(
                    disputes.insert().values(
                        dispute_id=seed_txns.DISPUTE_ID,
                        txn_id=t.txn_id,
                        customer_id=t.card.customer_id,
                        reason="WRONG_AMOUNT",
                        note="Charged twice the price on the menu board.",
                        status="OPEN",
                        created_at=t.occurred_at(now) + timedelta(days=1),
                    )
                )


# ---- Test support (TEST_MODE only) ----------------------------------------------------

test_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


@test_router.get(
    "/transactions/{trace_id}",
    summary="Read a transaction by trace_id (test support)",
    description="Any customer's transaction, with token_ref and the decision, for "
    "eventual-consistency checks after a simulated purchase.",
)
async def test_transaction(request: Request, trace_id: str = Path(max_length=64)) -> dict:
    async with request.app.state.runtime.db.session() as s:
        row = (
            await s.execute(
                sa.select(wallet_transactions).where(wallet_transactions.c.trace_id == trace_id)
            )
        ).first()
    if row is None:
        raise AppError(404, "TRANSACTION_NOT_FOUND", "No transaction with this trace_id")
    return dict(row._mapping)


app = create_app(
    ServiceSpec(
        name="wallet-txn",
        title="CardVault wallet-txn-service",
        description=(
            "System of record for wallet transactions: built from the auth.decided and "
            "txn.cleared events of wallet purchases, with the customer's transaction list, a "
            "CSV export and disputes."
        ),
        routers=[txn_router, dispute_router],
        test_routers=[test_router],
        uses_db=True,
        uses_kafka=True,
        reset_tables=RESET_TABLES,
        on_startup=[startup],
        on_seed=[seed],
    )
)
