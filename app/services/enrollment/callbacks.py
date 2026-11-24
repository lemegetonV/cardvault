"""``POST /v1/callbacks/operator``: signed status callbacks from the wallet operator.

1. The HMAC signature and the 5-minute timestamp window are checked first (401 otherwise).
2. The body is validated (422 on a bad shape).
3. ``event_id`` is recorded in ``processed_callbacks`` in the same transaction as the
   change, so a duplicate is acknowledged (200 ``DUPLICATE``) and changes nothing.
4. The callback is applied by ``domain.apply_callback``: stale ``operator_version``,
   callbacks for REMOVED enrolments and superseded callbacks are acknowledged and ignored.

The operator only needs a 2xx to stop retrying, so every outcome other than an
authentication or validation failure is a 200 with ``result`` saying what happened.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.dialects.postgresql import insert

from services.enrollment import domain
from services.enrollment.models import processed_callbacks
from shared import bugs, clock, hmac_auth
from shared.errors import AppError, ErrorBody

router = APIRouter(prefix="/v1/callbacks", tags=["callbacks"])

SECRET = os.environ.get("OPERATOR_HMAC_SECRET", "")


class OperatorCallback(BaseModel):
    event_id: uuid.UUID = Field(description="Unique per callback; used for deduplication")
    operator_ref: str | None = Field(None, max_length=64)
    enrollment_id: uuid.UUID
    status: Literal["ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]
    token_ref: str | None = Field(None, max_length=64)
    reason: str | None = Field(None, max_length=200)
    operator_version: int = Field(ge=0, description="Increases with every operator change")
    occurred_at: datetime


class CallbackAck(BaseModel):
    result: Literal[
        "APPLIED", "DUPLICATE", "STALE", "SUPERSEDED", "IGNORED_REMOVED", "FAILURE_RECORDED"
    ]
    enrollment_id: uuid.UUID
    status: str | None = Field(None, description="Enrolment status after the callback")


@router.post(
    "/operator",
    summary="Wallet operator status callback",
    description="HMAC-SHA256 over `{X-Timestamp}.{raw body}`, lowercase hex in X-Signature; "
    "X-Timestamp is Unix seconds within 5 minutes of the bank's clock. Deduplicated by "
    "event_id. A lower operator_version than the stored one is ignored as stale.",
    response_model=CallbackAck,
    responses={
        401: {"model": ErrorBody, "description": "Missing, wrong or expired signature"},
        404: {"model": ErrorBody, "description": "Unknown enrollment_id"},
        422: {"model": ErrorBody},
    },
)
async def operator_callback(request: Request) -> CallbackAck:
    raw = await request.body()
    # Seeded bugs: the signature, or only the timestamp window, is not checked.
    if not bugs.enabled("BUG_CALLBACK_SIGNATURE_IGNORED"):
        hmac_auth.verify(
            SECRET,
            request.headers.get(hmac_auth.TIMESTAMP_HEADER),
            request.headers.get(hmac_auth.SIGNATURE_HEADER),
            raw,
            check_timestamp=not bugs.enabled("BUG_CALLBACK_TIMESTAMP_NOT_CHECKED"),
        )
    try:
        cb = OperatorCallback.model_validate_json(raw)
    except ValidationError as exc:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "Callback body is not valid",
            [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()],
        ) from None

    runtime = request.app.state.runtime
    async with runtime.db.transaction() as s:
        enr = await domain.get_enrollment(s, enrollment_id=cb.enrollment_id, lock=True)
        if enr is None:
            raise AppError(404, "ENROLLMENT_NOT_FOUND", "No such enrolment")
        first = (
            await s.execute(
                insert(processed_callbacks)
                .values(
                    event_id=cb.event_id,
                    enrollment_id=cb.enrollment_id,
                    outcome="PROCESSING",
                    received_at=clock.now(),
                )
                .on_conflict_do_nothing()
                .returning(processed_callbacks.c.event_id)
            )
        ).first()
        if first is None:
            if not bugs.enabled("BUG_DUPLICATE_CALLBACK_APPLIED_TWICE"):
                return CallbackAck(
                    result="DUPLICATE", enrollment_id=enr.enrollment_id, status=enr.status
                )
            # Seeded bug: a redelivered callback writes history and publishes again.
            await domain.record_change(s, enr, "CALLBACK", enr.status_reason)
            result = domain.CallbackResult("APPLIED", enr)
        else:
            result = await domain.apply_callback(s, enr, cb)
            await s.execute(
                processed_callbacks.update()
                .where(processed_callbacks.c.event_id == cb.event_id)
                .values(outcome=result.outcome)
            )
    for relay in runtime.relays:
        relay.wake()
    return CallbackAck(
        result=result.outcome,  # type: ignore[arg-type]
        enrollment_id=cb.enrollment_id,
        status=result.enrollment.status,
    )
