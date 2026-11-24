"""Delivery of ``operator`` outbox rows: the bank's calls to the wallet operator.

* Each call is HMAC-signed like the callbacks (same scheme, the other direction) and
  carries ``Idempotency-Key = enrollment_id:version`` and the correlation ID.
* 202 is success. 409 means the operator already has this key: also success (rule 13).
* 5xx, timeouts and connection errors are retried with backoff, up to 5 attempts
  (``HTTP_BACKOFF_*``). Any other 4xx fails the push at once with the operator's reason.
* The ``operator_ref`` is read at delivery time, not at enqueue time: a suspend queued
  right behind an enrol only knows the reference once the enrol was accepted.
"""

from __future__ import annotations

import json
import os

import httpx
import sqlalchemy as sa

from services.enrollment import domain
from services.enrollment.models import enrollments
from shared import correlation, hmac_auth
from shared.db import Database
from shared.outbox import KindHandler, OutboxRow, PermanentDeliveryError
from shared.settings import BackoffSettings

OPERATOR_URL = os.environ.get("OPERATOR_INTERNAL_URL", "http://wallet-operator-mock:8101")
SECRET = os.environ.get("OPERATOR_HMAC_SECRET", "")
TIMEOUT = float(os.environ.get("OPERATOR_TIMEOUT_SECONDS", "3"))

ROUTES = {
    "ENROL": ("POST", "/v1/enrollments"),
    "UPDATE": ("PATCH", "/v1/enrollments/{ref}"),
    "CONTACT": ("PATCH", "/v1/enrollments/{ref}"),
    "SUSPEND": ("POST", "/v1/enrollments/{ref}/suspend"),
    "RESUME": ("POST", "/v1/enrollments/{ref}/resume"),
    "REMOVE": ("DELETE", "/v1/enrollments/{ref}"),
}


class OperatorRetryableError(RuntimeError):
    pass


def handler(db: Database, backoff: BackoffSettings) -> KindHandler:
    async def operator_ref(enrollment_id: str) -> str | None:
        async with db.session() as s:
            return await s.scalar(
                sa.select(enrollments.c.operator_ref).where(
                    enrollments.c.enrollment_id == enrollment_id
                )
            )

    async def deliver(row: OutboxRow) -> None:
        p = row.payload
        action = p["action"]
        method, path = ROUTES[action]
        if "{ref}" in path:
            ref = await operator_ref(p["enrollment_id"])
            if not ref:
                raise OperatorRetryableError("operator_ref not known yet")
            path = path.format(ref=ref)
        body = json.dumps(p["body"], separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": p["idempotency_key"],
            correlation.HEADER: correlation.get_or_new(),
            **hmac_auth.signature_headers(SECRET, body),
        }
        try:
            async with httpx.AsyncClient(base_url=OPERATOR_URL, timeout=TIMEOUT) as client:
                resp = await client.request(method, path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OperatorRetryableError(f"{action}: {type(exc).__name__}") from exc
        if resp.status_code in (200, 202):
            if action == "ENROL":
                ref = (resp.json() or {}).get("operator_ref")
                if ref:
                    async with db.transaction() as s:
                        await s.execute(
                            enrollments.update()
                            .where(
                                enrollments.c.enrollment_id == p["enrollment_id"],
                                enrollments.c.operator_ref.is_(None),
                            )
                            .values(operator_ref=ref)
                        )
            return
        if resp.status_code == 409:
            return  # the operator already has this Idempotency-Key
        reason = _reason(resp)
        if resp.status_code >= 500:
            raise OperatorRetryableError(f"{action}: operator {resp.status_code} {reason}")
        raise PermanentDeliveryError(f"OPERATOR_REJECTED: {resp.status_code} {reason}")

    async def on_give_up(session, row: OutboxRow, error: str) -> None:
        p = row.payload
        await domain.give_up(session, p["enrollment_id"], p["version"], p["action"], error)

    return KindHandler(deliver=deliver, backoff=backoff, on_give_up=on_give_up)


def _reason(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    return str(data.get("error_code") or data.get("reason") or "")[:200]
