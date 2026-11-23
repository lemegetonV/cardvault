"""Enrolment state machine: pushes to the operator, callbacks, and reactions to events.

Statuses: PENDING, ENROLLED, SUSPENDED, REMOVED, FAILED (spec 6.3, 16.1, 16.2).

* Every push to the wallet operator goes through the outbox (kind ``operator``) and
  increments ``version``; its ``Idempotency-Key`` is ``enrollment_id:version``.
* ``pending_action`` is the latest push still waiting for its callback. The operator's
  end state after all queued pushes is the *intent* (``intent()``). Reactions compare the
  card with the intent, so a reaction never pushes the same thing twice.
* SUSPENDED, operator-driven REMOVED and the resumed ENROLLED are set by callback. A
  customer remove or opt-out sets REMOVED at once, so authorization stops immediately.
* Every status (or token) change writes a history row and publishes ``enrollment.changed``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from services.enrollment import eligibility
from services.enrollment.models import (
    card_projection,
    contact_projection,
    enrollment_history,
    enrollments,
    outbox,
    wallet_card_prefs,
    wallet_prefs,
)
from shared import clock, correlation, events
from shared.outbox import enqueue, enqueue_event

log = logging.getLogger(__name__)

ACTIVE = ("PENDING", "ENROLLED", "SUSPENDED")
KIND_OPERATOR = "operator"

# Operator state once a push is applied.
INTENT = {
    "ENROL": "ENROLLED",
    "UPDATE": "ENROLLED",
    "CONTACT": "ENROLLED",
    "RESUME": "ENROLLED",
    "SUSPEND": "SUSPENDED",
    "REMOVE": "REMOVED",
}


def intent(enr) -> str:
    """Where the operator will be once every queued push is applied."""
    return INTENT[enr.pending_action] if enr.pending_action else enr.status


# ---- Reads --------------------------------------------------------------------------


async def get_enrollment(s: AsyncSession, *, card_id=None, enrollment_id=None, lock=False):
    q = sa.select(enrollments)
    q = q.where(
        enrollments.c.card_id == card_id
        if card_id is not None
        else enrollments.c.enrollment_id == enrollment_id
    )
    if lock:
        q = q.with_for_update()
    return (await s.execute(q)).first()


async def get_card(s: AsyncSession, card_id):
    return (
        await s.execute(sa.select(card_projection).where(card_projection.c.card_id == card_id))
    ).first()


async def get_contact(s: AsyncSession, customer_id):
    return (
        await s.execute(
            sa.select(contact_projection).where(contact_projection.c.customer_id == customer_id)
        )
    ).first()


async def get_prefs(s: AsyncSession, customer_id):
    return (
        await s.execute(sa.select(wallet_prefs).where(wallet_prefs.c.customer_id == customer_id))
    ).first()


async def card_removed_by_customer(s: AsyncSession, card_id) -> bool:
    return bool(
        await s.scalar(
            sa.select(wallet_card_prefs.c.opted_out).where(wallet_card_prefs.c.card_id == card_id)
        )
    )


async def check(
    s: AsyncSession, card, *, ignore_customer_removal: bool = False
) -> eligibility.Eligibility:
    prefs = await get_prefs(s, card.customer_id)
    return eligibility.evaluate(
        card,
        await get_contact(s, card.customer_id),
        opted_out=bool(prefs and prefs.opted_out),
        removed_by_customer=False
        if ignore_customer_removal
        else await card_removed_by_customer(s, card.card_id),
        now=clock.now(),
    )


# ---- Writes ---------------------------------------------------------------------------


async def _reload(s: AsyncSession, enrollment_id):
    return await get_enrollment(s, enrollment_id=enrollment_id)


async def _changed(s: AsyncSession, before_status: str | None, enr, source: str, reason) -> None:
    """History row plus enrollment.changed, for a status or token change."""
    now = clock.now()
    await s.execute(
        enrollment_history.insert().values(
            enrollment_id=enr.enrollment_id,
            from_status=before_status,
            to_status=enr.status,
            version=enr.version,
            operator_version=enr.operator_version,
            source=source,
            reason=reason,
            correlation_id=correlation.get(),
            created_at=now,
        )
    )
    payload = events.EnrollmentChangedPayload(
        enrollment_id=enr.enrollment_id,
        card_id=enr.card_id,
        customer_id=enr.customer_id,
        token_ref=enr.token_ref,
        status=enr.status,
        version=enr.version,
        operator_version=enr.operator_version,
    )
    await enqueue_event(
        s,
        outbox,
        events.ENROLLMENT_CHANGED,
        str(enr.card_id),
        events.make(events.ENROLLMENT_CHANGED, payload),
    )


async def _push(s: AsyncSession, enr, action: str, body: dict[str, Any], **values) -> Any:
    """Queue one operator call and bump the version. Returns the updated row."""
    version = enr.version + 1
    await s.execute(
        enrollments.update()
        .where(enrollments.c.enrollment_id == enr.enrollment_id)
        .values(version=version, pending_action=action, updated_at=clock.now(), **values)
    )
    await enqueue(
        s,
        outbox,
        kind=KIND_OPERATOR,
        topic=action,
        key=str(enr.enrollment_id),
        payload={
            "action": action,
            "enrollment_id": str(enr.enrollment_id),
            "version": version,
            "idempotency_key": f"{enr.enrollment_id}:{version}",
            "body": body,
        },
    )
    return await _reload(s, enr.enrollment_id)


def _contact_body(contact) -> dict[str, Any] | None:
    if contact is None:
        return None
    return {"email": contact.email, "phone": contact.phone}


async def enrol(s: AsyncSession, card, *, terms_version: str | None, source: str):
    """Start (or restart) enrolment for a card: PENDING now, ENROLLED by callback."""
    now = clock.now()
    enr = await get_enrollment(s, card_id=card.card_id, lock=True)
    before = enr.status if enr else None
    if enr is None:
        enrollment_id = uuid.uuid4()
        await s.execute(
            enrollments.insert().values(
                enrollment_id=enrollment_id,
                card_id=card.card_id,
                customer_id=card.customer_id,
                last4=card.last4,
                expiry=card.expiry,
                status="PENDING",
                version=0,
                operator_version=0,
                terms_version=terms_version,
                created_at=now,
                updated_at=now,
            )
        )
        enr = await _reload(s, enrollment_id)
    else:
        # Re-enrolment reuses the row; the operator issues a new token (rule 4).
        await s.execute(
            enrollments.update()
            .where(enrollments.c.enrollment_id == enr.enrollment_id)
            .values(
                status="PENDING",
                token_ref=None,
                last4=card.last4,
                expiry=card.expiry,
                failure_reason=None,
                status_reason=None,
                pending_reason=None,
                terms_version=terms_version or enr.terms_version,
                updated_at=now,
            )
        )
        enr = await _reload(s, enr.enrollment_id)
    contact = await get_contact(s, card.customer_id)
    enr = await _push(
        s,
        enr,
        "ENROL",
        {
            "enrollment_id": str(enr.enrollment_id),
            "card_id": str(card.card_id),
            "last4": card.last4,
            "expiry": card.expiry,
            "terms_version": enr.terms_version,
            "contact": _contact_body(contact),
        },
    )
    await _changed(s, before, enr, source, None)
    return enr


async def remove_now(s: AsyncSession, enr, *, reason: str, source: str):
    """Customer remove or opt-out: REMOVED at once, then tell the operator."""
    before = enr.status
    enr = await _push(s, enr, "REMOVE", {"reason": reason}, status="REMOVED", status_reason=reason)
    await _changed(s, before, enr, source, reason)
    return enr


async def request_remove(s: AsyncSession, enr, *, reason: str):
    """Bank-driven removal (card closed or no longer eligible): REMOVED comes by callback."""
    return await _push(s, enr, "REMOVE", {"reason": reason}, pending_reason=reason)


async def suspend(s: AsyncSession, enr, *, reason: str):
    return await _push(s, enr, "SUSPEND", {"reason": reason}, pending_reason=reason)


async def resume(s: AsyncSession, enr):
    return await _push(s, enr, "RESUME", {}, pending_reason=None)


async def update_card(s: AsyncSession, enr, card):
    """Reissue: the operator gets the new last4 and expiry (the card_id is unchanged)."""
    return await _push(
        s,
        enr,
        "UPDATE",
        {"last4": card.last4, "expiry": card.expiry},
        last4=card.last4,
        expiry=card.expiry,
    )


async def update_contact(s: AsyncSession, enr, contact):
    return await _push(s, enr, "CONTACT", {"contact": _contact_body(contact)})


# ---- Reactions --------------------------------------------------------------------------


async def reconcile_card(
    s: AsyncSession, card_id, *, source: str, allow_enrol: bool = False
) -> str | None:
    """Bring the enrolment of one card in line with the card and the customer.

    Called for card.lifecycle and contact.updated events (``allow_enrol=False``) and by the
    sweep (``allow_enrol=True``), which also enrols cards that became eligible. Returns the
    action queued, if any.
    """
    card = await get_card(s, card_id)
    if card is None:
        return None
    enr = await get_enrollment(s, card_id=card_id, lock=True)
    result = await check(s, card)
    if enr is not None and enr.status in ACTIVE:
        target = intent(enr)
        if card.status == "CLOSED":
            if target != "REMOVED":
                await request_remove(s, enr, reason="CARD_CLOSED")
                return "REMOVE"
            return None
        if card.status == "LOST":
            if target in ("ENROLLED", "PENDING"):
                await suspend(s, enr, reason="CARD_LOST")
                return "SUSPEND"
            return None
        if not result.eligible:
            if target != "REMOVED":
                await request_remove(s, enr, reason=result.reason or "INELIGIBLE")
                return "REMOVE"
            return None
        actions = []
        if target == "SUSPENDED":
            enr = await resume(s, enr)
            actions.append("RESUME")
        if (enr.last4, enr.expiry) != (card.last4, card.expiry) and intent(enr) != "REMOVED":
            await update_card(s, enr, card)
            actions.append("UPDATE")
        return "+".join(actions) or None
    if not allow_enrol or not result.eligible:
        return None
    if enr is not None and enr.status == "FAILED":
        today = clock.now().date()
        if enr.last_sweep_attempt_on == today:
            return None  # at most one sweep attempt per card per day (rule 5)
        await s.execute(
            enrollments.update()
            .where(enrollments.c.enrollment_id == enr.enrollment_id)
            .values(last_sweep_attempt_on=today)
        )
    prefs = await get_prefs(s, card.customer_id)
    await enrol(s, card, terms_version=prefs.terms_version if prefs else None, source=source)
    return "ENROL"


# ---- Callbacks --------------------------------------------------------------------------


@dataclass
class CallbackResult:
    outcome: str  # APPLIED | STALE | SUPERSEDED | IGNORED_REMOVED | FAILURE_RECORDED
    enrollment: Any


async def apply_callback(s: AsyncSession, enr, cb) -> CallbackResult:
    """Apply one operator callback to a locked enrolment row (spec 6.3, 16.2)."""
    if enr.status == "REMOVED":
        log.info(
            "callback for a REMOVED enrolment ignored",
            extra={"enrollment_id": str(enr.enrollment_id), "callback_status": cb.status},
        )
        return CallbackResult("IGNORED_REMOVED", enr)
    if cb.operator_version < enr.operator_version:
        log.info(
            "stale callback ignored",
            extra={
                "enrollment_id": str(enr.enrollment_id),
                "operator_version": cb.operator_version,
                "current_operator_version": enr.operator_version,
            },
        )
        return CallbackResult("STALE", enr)

    now = clock.now()
    base = {"operator_version": cb.operator_version, "updated_at": now}
    if cb.operator_ref:
        base["operator_ref"] = cb.operator_ref
    where = enrollments.c.enrollment_id == enr.enrollment_id

    if cb.status == "FAILED":
        reason = cb.reason or "FAILED_BY_OPERATOR"
        if enr.status == "PENDING":
            await s.execute(
                enrollments.update()
                .where(where)
                .values(
                    **base,
                    status="FAILED",
                    failure_reason=reason,
                    status_reason=reason,
                    pending_action=None,
                    pending_reason=None,
                )
            )
            updated = await _reload(s, enr.enrollment_id)
            await _changed(s, enr.status, updated, "CALLBACK", reason)
            return CallbackResult("APPLIED", updated)
        # A failed suspend, resume, update or remove keeps the status (rule 9). Clearing
        # pending_action lets the next event or sweep push the change again.
        await s.execute(
            enrollments.update()
            .where(where)
            .values(**base, failure_reason=reason, pending_action=None, pending_reason=None)
        )
        return CallbackResult("FAILURE_RECORDED", await _reload(s, enr.enrollment_id))

    target = intent(enr)
    if cb.status == "ENROLLED":
        applies = enr.status == "PENDING" or target == "ENROLLED"
    elif cb.status == "SUSPENDED":
        applies = enr.pending_action == "SUSPEND"
    else:  # REMOVED
        applies = enr.pending_action == "REMOVE"
    if not applies:
        log.info(
            "superseded callback ignored",
            extra={
                "enrollment_id": str(enr.enrollment_id),
                "callback_status": cb.status,
                "pending_action": enr.pending_action,
            },
        )
        # operator_version is not moved: only applied callbacks advance it.
        return CallbackResult("SUPERSEDED", enr)

    values: dict[str, Any] = dict(base, status=cb.status, failure_reason=None)
    if cb.status == "ENROLLED" and cb.token_ref:
        values["token_ref"] = cb.token_ref
    if INTENT.get(enr.pending_action or "") == cb.status:
        values.update(pending_action=None, pending_reason=None)
        if cb.status != "ENROLLED":
            values["status_reason"] = enr.pending_reason
    if cb.status == "ENROLLED":
        values["status_reason"] = None
    await s.execute(enrollments.update().where(where).values(**values))
    updated = await _reload(s, enr.enrollment_id)
    if (updated.status, updated.token_ref) != (enr.status, enr.token_ref):
        await _changed(s, enr.status, updated, "CALLBACK", updated.status_reason)
    return CallbackResult("APPLIED", updated)


async def give_up(s: AsyncSession, enrollment_id, version: int, action: str, error: str) -> None:
    """A push failed for good (5 attempts, or an operator 4xx)."""
    enr = await get_enrollment(s, enrollment_id=enrollment_id, lock=True)
    if enr is None or enr.version != version:
        # A newer push superseded this one; just keep the reason for support.
        if enr is not None:
            await s.execute(
                enrollments.update()
                .where(enrollments.c.enrollment_id == enrollment_id)
                .values(failure_reason=error[:500])
            )
        return
    if action == "ENROL" and enr.status == "PENDING":
        await s.execute(
            enrollments.update()
            .where(enrollments.c.enrollment_id == enrollment_id)
            .values(
                status="FAILED",
                failure_reason=error[:500],
                status_reason="OPERATOR_UNAVAILABLE",
                pending_action=None,
                pending_reason=None,
                updated_at=clock.now(),
            )
        )
        await _changed(s, "PENDING", await _reload(s, enrollment_id), "PUSH", error[:500])
        return
    await s.execute(
        enrollments.update()
        .where(enrollments.c.enrollment_id == enrollment_id)
        .values(
            failure_reason=error[:500],
            pending_action=None,
            pending_reason=None,
            updated_at=clock.now(),
        )
    )


# ---- Projections ------------------------------------------------------------------------


async def upsert_card(s: AsyncSession, p: events.CardLifecyclePayload | Any, now: datetime) -> None:
    stmt = insert(card_projection).values(
        card_id=p.card_id,
        customer_id=p.customer_id,
        product=p.product,
        status=p.status,
        last4=p.last4,
        expiry=p.expiry,
        last_used_at=p.last_used_at,
        updated_at=now,
    )
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=[card_projection.c.card_id],
            set_={
                c: getattr(stmt.excluded, c)
                for c in ("product", "status", "last4", "expiry", "last_used_at", "updated_at")
            },
        )
    )


async def upsert_contact(s: AsyncSession, p: events.ContactUpdatedPayload, now: datetime) -> bool:
    """Apply a contact.updated event if it is newer. Returns True when it changed anything."""
    stmt = insert(contact_projection).values(
        customer_id=p.customer_id,
        email=p.email,
        email_verified=p.email_verified,
        phone=p.phone,
        phone_verified=p.phone_verified,
        version=p.version,
        updated_at=now,
    )
    result = await s.execute(
        stmt.on_conflict_do_update(
            index_elements=[contact_projection.c.customer_id],
            set_={
                c: getattr(stmt.excluded, c)
                for c in (
                    "email",
                    "email_verified",
                    "phone",
                    "phone_verified",
                    "version",
                    "updated_at",
                )
            },
            where=contact_projection.c.version < stmt.excluded.version,
        ).returning(contact_projection.c.customer_id)
    )
    return result.first() is not None


async def customer_card_ids(s: AsyncSession, customer_id) -> list:
    return list(
        await s.scalars(
            sa.select(card_projection.c.card_id).where(card_projection.c.customer_id == customer_id)
        )
    )
