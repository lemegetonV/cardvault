"""profile-service customer API: contact details and their verification.

* ``PATCH /v1/profile`` changes email or phone. A changed value is unverified again; setting
  the same value keeps its flag (spec 16.5 rule 35). ``If-Match: <version>`` gives
  optimistic locking: a stale version returns 412.
* ``POST /v1/profile/verify/send`` "sends" a one-time code, at most once every 30 seconds
  per channel on the shared clock (rule 52).
* ``POST /v1/profile/verify`` checks the code. Under TEST_MODE the code is always
  ``000000``. Five wrong codes lock that channel for 15 minutes.

Every change that touches contact data publishes ``contact.updated`` (keyed by customer_id)
through the outbox. enrollment-service uses it to push identity updates to the wallet
operator and to re-check eligibility, because an unverified contact makes cards ineligible.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.dialects.postgresql import insert

from services.profile.models import idempotency_keys, otp_state, outbox, profiles
from shared import clock, correlation, events, idempotency
from shared.errors import AppError, ErrorBody
from shared.outbox import enqueue_event
from shared.security import Customer, require_customer
from shared.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/profile", tags=["profile"])

TEST_MODE_CODE = "000000"
OTP_COOLDOWN = timedelta(seconds=30)
OTP_MAX_FAILURES = 5
OTP_LOCK = timedelta(minutes=15)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

Channel = Literal["EMAIL", "PHONE"]
ERRORS = {401: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class ProfileView(BaseModel):
    customer_id: str
    email: str | None = Field(description="Email address")
    email_verified: bool
    phone: str | None = Field(description="Phone number in E.164 format, e.g. +15550100101")
    phone_verified: bool
    version: int = Field(description="Send it back in If-Match to update safely")
    updated_at: datetime


class ProfilePatch(BaseModel):
    """Only the fields that are sent change."""

    email: str | None = Field(None, max_length=254, description="New email address")
    phone: str | None = Field(None, description="New phone number, E.164 (e.g. +15550100101)")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is not None and not EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v.strip().lower() if v else v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is not None and not E164_RE.match(v):
            raise ValueError("phone must be E.164, e.g. +15550100101")
        return v


class SendRequest(BaseModel):
    channel: Channel = Field(description="Which contact to verify: EMAIL or PHONE")


class SendResponse(BaseModel):
    channel: Channel
    sent_to: str = Field(description="Masked destination")
    cooldown_seconds: int = Field(description="Seconds before another code can be requested")


class VerifyRequest(BaseModel):
    channel: Channel = Field(description="EMAIL or PHONE")
    code: str = Field(pattern=r"^\d{6}$", description="The 6-digit code (000000 in TEST_MODE)")


def view(row) -> ProfileView:
    return ProfileView(
        customer_id=str(row.customer_id),
        email=row.email,
        email_verified=row.email_verified,
        phone=row.phone,
        phone_verified=row.phone_verified,
        version=row.version,
        updated_at=row.updated_at,
    )


def _db(request: Request):
    return request.app.state.runtime.db


def _wake(request: Request) -> None:
    for relay in request.app.state.runtime.relays:
        relay.wake()


async def _load(session, customer_id, lock: bool = False):
    q = sa.select(profiles).where(profiles.c.customer_id == customer_id)
    if lock:
        q = q.with_for_update()
    row = (await session.execute(q)).first()
    if row is None:
        raise AppError(404, "PROFILE_NOT_FOUND", "No profile for this customer")
    return row


async def publish_contact(session, row) -> None:
    """Enqueue contact.updated for the profile as it is now (same transaction)."""
    payload = events.ContactUpdatedPayload(
        customer_id=row.customer_id,
        email=row.email,
        email_verified=row.email_verified,
        phone=row.phone,
        phone_verified=row.phone_verified,
        version=row.version,
    )
    await enqueue_event(
        session,
        outbox,
        events.CONTACT_UPDATED,
        str(row.customer_id),
        events.make(events.CONTACT_UPDATED, payload),
    )


def _etag(version: int) -> dict[str, str]:
    return {"ETag": f'"{version}"'}


def _parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        raise AppError(
            400, "INVALID_IF_MATCH", "If-Match must be the profile version, e.g. 3"
        ) from None


@router.get(
    "",
    summary="Get my profile",
    description="Contact details and their verification flags. The ETag is the version.",
    response_model=ProfileView,
    responses=ERRORS,
)
async def get_profile(request: Request, customer: Customer = Depends(require_customer)):
    async with _db(request).session() as s:
        row = await _load(s, customer.customer_id)
    return JSONResponse(view(row).model_dump(mode="json"), headers=_etag(row.version))


@router.patch(
    "",
    summary="Change my email or phone",
    description="A changed email or phone must be verified again; the same value keeps its "
    "flag. Send `If-Match: <version>` to avoid overwriting a newer change: a mismatch returns "
    "412 VERSION_MISMATCH. Publishes contact.updated when anything changed.",
    response_model=ProfileView,
    responses={**ERRORS, 412: {"model": ErrorBody}},
)
async def patch_profile(
    body: ProfilePatch,
    request: Request,
    customer: Customer = Depends(require_customer),
    if_match: str | None = Header(None, description="The version you last read"),
):
    expected = _parse_if_match(if_match)
    async with _db(request).transaction() as s:
        row = await _load(s, customer.customer_id, lock=True)
        if expected is not None and expected != row.version:
            raise AppError(
                412,
                "VERSION_MISMATCH",
                "The profile changed since you read it",
                {"current_version": row.version},
            )
        changes: dict = {}
        if body.email is not None and body.email != row.email:
            changes.update(email=body.email, email_verified=False)
        if body.phone is not None and body.phone != row.phone:
            changes.update(phone=body.phone, phone_verified=False)
        if changes:
            await s.execute(
                profiles.update()
                .where(profiles.c.customer_id == customer.customer_id)
                .values(**changes, version=row.version + 1, updated_at=clock.now())
            )
            row = await _load(s, customer.customer_id)
            await publish_contact(s, row)
    if changes:
        _wake(request)
    return JSONResponse(view(row).model_dump(mode="json"), headers=_etag(row.version))


def _mask(channel: str, row) -> str:
    if channel == "EMAIL":
        local, _, domain = (row.email or "").partition("@")
        return f"{local[:1]}***@{domain}"
    return f"***{(row.phone or '')[-2:]}"


def _contact(channel: str, row) -> tuple[str | None, bool]:
    return (
        (row.email, row.email_verified) if channel == "EMAIL" else (row.phone, row.phone_verified)
    )


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def _otp_row(session, customer_id, channel: str):
    await session.execute(
        insert(otp_state)
        .values(customer_id=customer_id, channel=channel, failed_attempts=0)
        .on_conflict_do_nothing()
    )
    return (
        await session.execute(
            sa.select(otp_state)
            .where(otp_state.c.customer_id == customer_id, otp_state.c.channel == channel)
            .with_for_update()
        )
    ).one()


def _otp_where(customer_id, channel: str):
    return sa.and_(otp_state.c.customer_id == customer_id, otp_state.c.channel == channel)


@router.post(
    "/verify/send",
    summary="Send a verification code",
    description="Sends a one-time code to the email or phone. At most one code every 30 "
    "seconds per channel (429 OTP_COOLDOWN with details.retry_after_seconds). Under TEST_MODE "
    "nothing is delivered and the code is always 000000." + " Accepts `Idempotency-Key`.",
    status_code=202,
    response_model=SendResponse,
    responses={
        **ERRORS,
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
        429: {"model": ErrorBody},
    },
)
async def send_code(
    body: SendRequest, request: Request, customer: Customer = Depends(require_customer)
):
    async def handler():
        now = clock.now()
        async with _db(request).transaction() as s:
            row = await _load(s, customer.customer_id)
            value, verified = _contact(body.channel, row)
            if not value:
                raise AppError(422, "CONTACT_MISSING", f"No {body.channel.lower()} to verify")
            if verified:
                raise AppError(409, "ALREADY_VERIFIED", f"The {body.channel.lower()} is verified")
            otp = await _otp_row(s, customer.customer_id, body.channel)
            if otp.last_sent_at is not None and now - otp.last_sent_at < OTP_COOLDOWN:
                wait = OTP_COOLDOWN - (now - otp.last_sent_at)
                raise AppError(
                    429,
                    "OTP_COOLDOWN",
                    "A code was sent recently; wait before asking again",
                    {"retry_after_seconds": max(1, int(wait.total_seconds() + 0.999))},
                )
            code_hash = None
            if not get_settings().test_mode:
                # No delivery channel exists in this simulation; the code is only stored.
                code_hash = _hash(f"{secrets.randbelow(1_000_000):06d}")
            await s.execute(
                otp_state.update()
                .where(_otp_where(customer.customer_id, body.channel))
                .values(last_sent_at=now, code_hash=code_hash)
            )
        log.info("verification code sent", extra={"channel": body.channel})
        return 202, SendResponse(
            channel=body.channel,
            sent_to=_mask(body.channel, row),
            cooldown_seconds=int(OTP_COOLDOWN.total_seconds()),
        )

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


@router.post(
    "/verify",
    summary="Verify my email or phone with a code",
    description="Marks the channel verified when the code is right (000000 under TEST_MODE) "
    "and publishes contact.updated. A wrong code returns 422 OTP_INVALID with "
    "details.attempts_remaining; the fifth wrong code locks the channel for 15 minutes "
    "(423 OTP_LOCKED). An already verified channel returns 200 unchanged."
    " Accepts `Idempotency-Key`.",
    response_model=ProfileView,
    responses={**ERRORS, 422: {"model": ErrorBody}, 423: {"model": ErrorBody}},
)
async def verify(
    body: VerifyRequest, request: Request, customer: Customer = Depends(require_customer)
):
    async def handler():
        now = clock.now()
        async with _db(request).transaction() as s:
            row = await _load(s, customer.customer_id, lock=True)
            value, verified = _contact(body.channel, row)
            if not value:
                raise AppError(422, "CONTACT_MISSING", f"No {body.channel.lower()} to verify")
            if verified:
                return 200, view(row)
            otp = await _otp_row(s, customer.customer_id, body.channel)
            if otp.locked_until is not None and now < otp.locked_until:
                raise AppError(
                    423,
                    "OTP_LOCKED",
                    "Too many wrong codes; try again later",
                    {"locked_until": otp.locked_until.isoformat()},
                )
            if get_settings().test_mode:
                good = hmac.compare_digest(body.code, TEST_MODE_CODE)
            else:
                good = otp.code_hash is not None and hmac.compare_digest(
                    _hash(body.code), otp.code_hash
                )
            if not good:
                failures = (0 if otp.locked_until is not None else otp.failed_attempts) + 1
                locked = failures >= OTP_MAX_FAILURES
                await s.execute(
                    otp_state.update()
                    .where(_otp_where(customer.customer_id, body.channel))
                    .values(
                        failed_attempts=0 if locked else failures,
                        locked_until=now + OTP_LOCK if locked else None,
                    )
                )
                if locked:
                    return 423, _error(
                        "OTP_LOCKED",
                        "Too many wrong codes; try again later",
                        {"locked_until": (now + OTP_LOCK).isoformat()},
                    )
                return 422, _error(
                    "OTP_INVALID",
                    "The code is not correct",
                    {"attempts_remaining": OTP_MAX_FAILURES - failures},
                )
            flag = "email_verified" if body.channel == "EMAIL" else "phone_verified"
            await s.execute(
                profiles.update()
                .where(profiles.c.customer_id == customer.customer_id)
                .values({flag: True, "version": row.version + 1, "updated_at": now})
            )
            await s.execute(
                otp_state.update()
                .where(_otp_where(customer.customer_id, body.channel))
                .values(failed_attempts=0, locked_until=None, code_hash=None)
            )
            row = await _load(s, customer.customer_id)
            await publish_contact(s, row)
        _wake(request)
        return 200, view(row)

    return await idempotency.run(
        _db(request), idempotency_keys, request, str(customer.customer_id), handler
    )


def _error(code: str, message: str, details: dict) -> ErrorBody:
    """A 4xx body returned (not raised), so the wrong-code counter commits with it."""
    return ErrorBody(
        error_code=code, message=message, correlation_id=correlation.get(), details=details
    )
