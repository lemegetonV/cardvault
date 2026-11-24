"""enrollment-service application: wiring, event consumers, sweep, seed and test APIs."""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Literal

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from services.enrollment import domain, operator_client
from services.enrollment.api import router as wallet_router
from services.enrollment.callbacks import router as callback_router
from services.enrollment.models import (
    RESET_TABLES,
    card_projection,
    contact_projection,
    enrollment_history,
    enrollments,
    outbox,
    processed_events,
    wallet_prefs,
)
from services.enrollment.recon import router as recon_router
from shared import clock, events, http_client, seed_data
from shared.errors import AppError
from shared.kafka import Consumer
from shared.outbox import OutboxRelay, kafka_event_handler
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_service, require_test_key
from shared.settings import get_settings

log = logging.getLogger(__name__)

CONSUMER_GROUP = "enrollment-service"


def _wake(runtime: Runtime) -> None:
    for relay in runtime.relays:
        relay.wake()


# ---- Event consumers -------------------------------------------------------------------


async def handle_event(runtime: Runtime, envelope: events.Envelope) -> None:
    """card.lifecycle and contact.updated. Deduplicated by event_id in the same transaction."""
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
        p = envelope.payload
        if envelope.event_type == events.CARD_LIFECYCLE:
            await domain.upsert_card(s, p, now)
            await domain.reconcile_card(s, p.card_id, source=f"EVENT:{p.event}")
        elif envelope.event_type == events.CONTACT_UPDATED:
            if not await domain.upsert_contact(s, p, now):
                return  # an older or repeated version
            contact = await domain.get_contact(s, p.customer_id)
            for card_id in await domain.customer_card_ids(s, p.customer_id):
                action = await domain.reconcile_card(s, card_id, source="EVENT:CONTACT_UPDATED")
                enr = await domain.get_enrollment(s, card_id=card_id, lock=True)
                if action is None and enr is not None and domain.intent(enr) == "ENROLLED":
                    # Still eligible: the operator gets the new contact details.
                    await domain.update_contact(s, enr, contact)
    _wake(runtime)


async def startup(runtime: Runtime) -> None:
    s = get_settings()
    runtime.relays.append(
        OutboxRelay(
            runtime.db,
            outbox,
            {"event": kafka_event_handler(runtime.producer, s.event_backoff)},
            kinds=["event"],
        )
    )
    runtime.relays.append(
        OutboxRelay(
            runtime.db,
            outbox,
            {domain.KIND_OPERATOR: operator_client.handler(runtime.db, s.http_backoff)},
            kinds=[domain.KIND_OPERATOR],
        )
    )

    async def handler(envelope: events.Envelope) -> None:
        await handle_event(runtime, envelope)

    runtime.consumers.append(
        Consumer(
            bootstrap=s.kafka_bootstrap,
            group_id=CONSUMER_GROUP,
            topics=[events.CARD_LIFECYCLE, events.CONTACT_UPDATED],
            handler=handler,
            dlq_topic=events.ENROLLMENT_DLQ,
            producer=runtime.producer,
            backoff=s.event_backoff,
        )
    )


# ---- Seed ---------------------------------------------------------------------------------


async def seed(runtime: Runtime) -> None:
    """Write the seed state directly, projections included (spec 16.7 rule 47)."""
    now = clock.now()
    async with runtime.db.transaction() as s:
        for customer_id, c in seed_data.CONTACTS.items():
            await s.execute(
                contact_projection.insert().values(
                    customer_id=customer_id,
                    email=c.email,
                    email_verified=c.email_verified,
                    phone=c.phone,
                    phone_verified=c.phone_verified,
                    version=c.version,
                    updated_at=now,
                )
            )
            await s.execute(
                wallet_prefs.insert().values(
                    customer_id=customer_id,
                    opted_out=customer_id in seed_data.OPTED_OUT,
                    terms_version=seed_data.TERMS_VERSION,
                    updated_at=now,
                )
            )
        for card in seed_data.CARDS:
            # An upsert: card-service's CARD_ACTIVATED for the same card may already be here.
            await domain.upsert_card(
                s,
                SimpleNamespace(
                    card_id=card.card_id,
                    customer_id=card.customer_id,
                    product=card.product,
                    status=card.status,
                    last4=card.last4,
                    expiry=card.expiry,
                    last_used_at=None
                    if card.used_days_ago is None
                    else now - timedelta(days=card.used_days_ago),
                ),
                now,
            )
            e = card.enrollment
            if e is None:
                continue
            reason = {"SUSPENDED": "CARD_LOST", "REMOVED": "CUSTOMER_OPTED_OUT"}.get(e.status)
            await s.execute(
                enrollments.insert().values(
                    enrollment_id=e.enrollment_id,
                    card_id=card.card_id,
                    customer_id=card.customer_id,
                    operator_ref=seed_data.operator_ref(e.enrollment_id),
                    token_ref=e.token_ref,
                    last4=card.last4,
                    expiry=card.expiry,
                    status=e.status,
                    version=e.version,
                    operator_version=e.version,
                    status_reason=reason,
                    terms_version=seed_data.TERMS_VERSION,
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.execute(
                enrollment_history.insert().values(
                    enrollment_id=e.enrollment_id,
                    from_status=None,
                    to_status=e.status,
                    version=e.version,
                    operator_version=e.version,
                    source="SEED",
                    reason=reason,
                    created_at=now,
                )
            )


# ---- Internal API (tests key) ------------------------------------------------------------

internal_router = APIRouter(prefix="/internal/v1", tags=["internal"])


class SweepResult(BaseModel):
    cards_checked: int
    actions: list[dict] = Field(description="card_id and the action queued for it")


@internal_router.post(
    "/sweep",
    summary="Run the daily eligibility sweep now",
    description="Checks every card: enrols cards that became eligible (FAILED ones at most "
    "once a day), removes enrolments that became ineligible, suspends or resumes as the card "
    "status requires. TEST_MODE only; needs the tests X-Service-Key.",
    response_model=SweepResult,
)
async def sweep(request: Request, _: str = Depends(require_service("tests"))) -> SweepResult:
    runtime = request.app.state.runtime
    async with runtime.db.session() as s:
        card_ids = list(await s.scalars(sa.select(card_projection.c.card_id)))
    actions = []
    for card_id in card_ids:
        async with runtime.db.transaction() as s:
            action = await domain.reconcile_card(s, card_id, source="SWEEP", allow_enrol=True)
        if action:
            actions.append({"card_id": str(card_id), "action": action})
    _wake(runtime)
    return SweepResult(cards_checked=len(card_ids), actions=actions)


# ---- Test support -------------------------------------------------------------------------

test_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)

AUTH_URL = os.environ.get("AUTH_URL", "http://auth:8001")
CARD_URL = os.environ.get("CARD_URL", "http://card:8002")
PROFILE_URL = os.environ.get("PROFILE_URL", "http://profile:8005")
NETWORK_URL = os.environ.get("NETWORK_URL", "http://network-mock:8102")
OPERATOR_URL = operator_client.OPERATOR_URL


class EnrollmentRow(BaseModel):
    enrollment_id: uuid.UUID
    card_id: uuid.UUID
    customer_id: uuid.UUID
    operator_ref: str | None
    token_ref: str | None
    last4: str
    expiry: str
    status: str
    version: int
    operator_version: int
    pending_action: str | None
    status_reason: str | None
    failure_reason: str | None


@test_router.get(
    "/enrollments",
    summary="Enrolment rows, token_ref included (test support)",
    response_model=list[EnrollmentRow],
)
async def list_enrollments(
    request: Request,
    customer_id: uuid.UUID | None = Query(None),
    card_id: uuid.UUID | None = Query(None),
) -> list[EnrollmentRow]:
    q = sa.select(enrollments).order_by(enrollments.c.card_id)
    if customer_id:
        q = q.where(enrollments.c.customer_id == customer_id)
    if card_id:
        q = q.where(enrollments.c.card_id == card_id)
    async with request.app.state.runtime.db.session() as s:
        rows = (await s.execute(q)).all()
    return [EnrollmentRow.model_validate(dict(r._mapping)) for r in rows]


@test_router.get("/enrollments/{enrollment_id}/history", summary="Audit trail (test support)")
async def history(enrollment_id: uuid.UUID, request: Request) -> list[dict]:
    async with request.app.state.runtime.db.session() as s:
        rows = (
            await s.execute(
                sa.select(enrollment_history)
                .where(enrollment_history.c.enrollment_id == enrollment_id)
                .order_by(enrollment_history.c.id)
            )
        ).all()
    return [
        {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r._mapping.items()}
        for r in rows
    ]


class TemplateCard(BaseModel):
    product: Literal["CREDIT", "DEBIT", "PREPAID"] = "CREDIT"
    status: Literal["ACTIVE", "LOST", "CLOSED"] = "ACTIVE"
    used_days_ago: int | None = Field(5, ge=0, description="null means never used")
    enrolled: bool = Field(False, description="Create it already ENROLLED in the wallet")
    limit: int = Field(300_000, ge=0, description="Cents")
    available: int | None = Field(None, description="Cents; defaults to limit")
    expiry: str | None = Field(None, pattern=r"^\d{4}-\d{2}$")


class CustomerTemplate(BaseModel):
    username: str | None = Field(None, description="Generated when omitted")
    password: str | None = Field(None, description="Generated when omitted")
    email_verified: bool = True
    phone_verified: bool = True
    opted_out: bool = False
    cards: list[TemplateCard] = Field(default_factory=list, max_length=20)


class CreatedCard(BaseModel):
    card_id: uuid.UUID
    product: str
    status: str
    last4: str
    enrollment_id: uuid.UUID | None
    enrollment_status: str | None
    token_ref: str | None


class CreatedCustomer(BaseModel):
    username: str
    password: str
    customer_id: uuid.UUID
    cards: list[CreatedCard]


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 300:
        raise AppError(
            502,
            "ORCHESTRATION_FAILED",
            f"{what} failed with {resp.status_code}",
            {"upstream": resp.text[:500]},
        )
    return resp.json()


@test_router.post(
    "/customers",
    summary="Create a fresh customer with cards (test orchestrator)",
    description="Creates the login (auth), profile (profile-service), cards (card-service) and, "
    "for cards with enrolled=true, a token at the network, a record at the wallet operator, an "
    "ENROLLED enrolment here and the token in card-service. Returns the credentials, "
    "customer_id, every card_id and every token_ref.",
    status_code=201,
    response_model=CreatedCustomer,
)
async def create_customer(body: CustomerTemplate, request: Request) -> CreatedCustomer:
    runtime = request.app.state.runtime
    s = get_settings()
    customer_id = uuid.uuid4()
    username = body.username or f"cust-{customer_id.hex[:10]}"
    password = body.password or f"Test-{secrets.token_urlsafe(12)}"
    test_headers = {"X-Test-Key": s.test_key}
    tests_key = s.service_keys.get("tests", "")

    async with http_client.client(AUTH_URL) as c:
        _check(
            await c.post(
                "/test/users",
                json={"username": username, "password": password, "customer_id": str(customer_id)},
                headers=test_headers,
            ),
            "auth /test/users",
        )
    async with http_client.client(PROFILE_URL) as c:
        profile = _check(
            await c.post(
                "/test/profiles",
                json={
                    "customer_id": str(customer_id),
                    "email_verified": body.email_verified,
                    "phone_verified": body.phone_verified,
                },
                headers=test_headers,
            ),
            "profile /test/profiles",
        )
    created_cards = []
    async with http_client.client(CARD_URL) as c:
        for tc in body.cards:
            created_cards.append(
                (
                    tc,
                    _check(
                        await c.post(
                            "/test/cards",
                            json={
                                "customer_id": str(customer_id),
                                "product": tc.product,
                                "status": tc.status,
                                "limit": tc.limit,
                                "available": tc.available,
                                "expiry": tc.expiry,
                                "used_days_ago": tc.used_days_ago,
                            },
                            headers=test_headers,
                        ),
                        "card /test/cards",
                    ),
                )
            )

    now = clock.now()
    out: list[CreatedCard] = []
    async with runtime.db.transaction() as db:
        await db.execute(
            insert(contact_projection)
            .values(
                customer_id=customer_id,
                email=profile["email"],
                email_verified=profile["email_verified"],
                phone=profile["phone"],
                phone_verified=profile["phone_verified"],
                version=profile["version"],
                updated_at=now,
            )
            .on_conflict_do_nothing()
        )
        await db.execute(
            wallet_prefs.insert().values(
                customer_id=customer_id,
                opted_out=body.opted_out,
                terms_version=seed_data.TERMS_VERSION,
                updated_at=now,
            )
        )
        for _, card in created_cards:
            await db.execute(
                insert(card_projection)
                .values(
                    card_id=uuid.UUID(card["card_id"]),
                    customer_id=customer_id,
                    product=card["product"],
                    status=card["status"],
                    last4=card["last4"],
                    expiry=card["expiry"],
                    last_used_at=datetime.fromisoformat(card["last_used_at"])
                    if card["last_used_at"]
                    else None,
                    updated_at=now,
                )
                .on_conflict_do_nothing()
            )

    for tc, card in created_cards:
        enrollment_id = token_ref = status = None
        if tc.enrolled:
            enrollment_id = uuid.uuid4()
            token_ref, status = await _enrol_directly(
                runtime, customer_id, enrollment_id, card, tests_key, test_headers
            )
        out.append(
            CreatedCard(
                card_id=card["card_id"],
                product=card["product"],
                status=card["status"],
                last4=card["last4"],
                enrollment_id=enrollment_id,
                enrollment_status=status,
                token_ref=token_ref,
            )
        )
    return CreatedCustomer(username=username, password=password, customer_id=customer_id, cards=out)


async def _enrol_directly(runtime, customer_id, enrollment_id, card, tests_key, test_headers):
    """An ENROLLED card without the async flow: token, operator record, SOR row, card copy."""
    async with http_client.client(NETWORK_URL, service_key=tests_key) as c:
        token = _check(
            await c.post(
                "/v1/tokens",
                json={"card_id": card["card_id"], "enrollment_id": str(enrollment_id)},
            ),
            "network /v1/tokens",
        )
    token_ref = token["token_ref"]
    operator_ref = seed_data.operator_ref(enrollment_id)
    async with http_client.client(OPERATOR_URL) as c:
        _check(
            await c.post(
                "/test/state",
                json={
                    "upsert": [
                        {
                            "enrollment_id": str(enrollment_id),
                            "operator_ref": operator_ref,
                            "card_id": card["card_id"],
                            "token_ref": token_ref,
                            "status": "ENROLLED",
                            "last4": card["last4"],
                            "expiry": card["expiry"],
                            "operator_version": 1,
                        }
                    ]
                },
                headers=test_headers,
            ),
            "operator /test/state",
        )
    now = clock.now()
    async with runtime.db.transaction() as db:
        await db.execute(
            enrollments.insert().values(
                enrollment_id=enrollment_id,
                card_id=uuid.UUID(card["card_id"]),
                customer_id=customer_id,
                operator_ref=operator_ref,
                token_ref=token_ref,
                last4=card["last4"],
                expiry=card["expiry"],
                status="ENROLLED",
                version=1,
                operator_version=1,
                terms_version=seed_data.TERMS_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
        await db.execute(
            enrollment_history.insert().values(
                enrollment_id=enrollment_id,
                from_status=None,
                to_status="ENROLLED",
                version=1,
                operator_version=1,
                source="TEST_CUSTOMER",
                created_at=now,
            )
        )
    async with http_client.client(CARD_URL) as c:
        _check(
            await c.post(
                "/test/wallet-tokens",
                json={
                    "token_ref": token_ref,
                    "card_id": card["card_id"],
                    "enrollment_id": str(enrollment_id),
                    "status": "ENROLLED",
                    "version": 1,
                },
                headers=test_headers,
            ),
            "card /test/wallet-tokens",
        )
    return token_ref, "ENROLLED"


app = create_app(
    ServiceSpec(
        name="enrollment",
        title="CardVault enrollment-service",
        description=(
            "Wallet enrolment system of record for CardVault Bank: card eligibility, enrolment "
            "with the wallet operator (pushed through the outbox, confirmed by signed "
            "callbacks), reactions to card and contact changes, and the customer's wallet "
            "choices (opt-out, opt-in, add and remove a card)."
        ),
        routers=[wallet_router, callback_router, recon_router],
        test_routers=[test_router, internal_router],
        uses_db=True,
        uses_kafka=True,
        reset_tables=RESET_TABLES,
        on_startup=[startup],
        on_seed=[seed],
    )
)
