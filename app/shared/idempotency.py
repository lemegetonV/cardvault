"""``Idempotency-Key`` handling (spec section 5 and 16.5 rule 32).

* The header is optional. Without it the request runs normally.
* Scope: the caller (customer ID or service name) plus the route template, so one
  customer's key can never replay another customer's response.
* The stored request fingerprint is the method, the concrete path and the body. A key
  reused for a different fingerprint returns 422 ``IDEMPOTENCY_KEY_REUSED``.
* A key whose first request is still running returns 409 ``IDEMPOTENCY_IN_PROGRESS``.
* A finished key replays the stored status and body (header ``Idempotent-Replayed: true``).
  Client errors (4xx) are stored and replayed too; server errors are not, so the client
  can retry them.
* Keys expire after 24 hours on the shared clock; ``/test/reset`` truncates the table.

The key row and the business change are committed in separate transactions. If the
process dies in between, the row stays ``IN_PROGRESS``; after 60 seconds such a row is
treated as abandoned and the next request with that key runs again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy.dialects.postgresql import insert

from shared import bugs, clock, correlation
from shared.db import Database
from shared.errors import AppError, ErrorBody

HEADER = "Idempotency-Key"
RETENTION = timedelta(hours=24)
ABANDONED_AFTER = timedelta(seconds=60)
MAX_KEY_LENGTH = 128


def _columns() -> list[sa.Column]:
    return [
        sa.Column("scope", sa.String(300), primary_key=True),
        sa.Column("key", sa.String(MAX_KEY_LENGTH), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body", sa.Text),  # exact bytes, so a replay is byte-identical
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def idempotency_table(metadata: sa.MetaData, name: str = "idempotency_keys") -> sa.Table:
    return sa.Table(name, metadata, *_columns())


def create_idempotency_table(op, name: str = "idempotency_keys") -> None:
    op.create_table(name, *_columns())


Handler = Callable[[], Awaitable[tuple[int, Any]]]


async def run(
    db: Database,
    table: sa.Table,
    request: Request,
    caller: str,
    handler: Handler,
    fingerprint_body: bytes | None = None,
) -> Response:
    """Run ``handler`` (which returns ``(status, body)``) under the request's key, if any.

    ``fingerprint_body`` replaces the raw body in the fingerprint. Multipart requests need
    it: the boundary is random, so a client's retry never has the same raw bytes.
    """
    key = request.headers.get(HEADER)
    if not key:
        status, body = await handler()
        return JSONResponse(status_code=status, content=jsonable_encoder(body))
    if len(key) > MAX_KEY_LENGTH:
        raise AppError(422, "VALIDATION_ERROR", f"{HEADER} is longer than {MAX_KEY_LENGTH}")

    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    # Seeded bug hook (phase 6): without the caller in the scope, keys leak across callers.
    scope_owner = "" if bugs.enabled("BUG_IDEMPOTENCY_KEY_NOT_SCOPED") else caller
    scope = f"{scope_owner}|{request.method} {template}"
    raw = fingerprint_body if fingerprint_body is not None else await request.body()
    fingerprint = hashlib.sha256(
        request.method.encode() + b" " + request.url.path.encode() + b"\n" + raw
    ).hexdigest()
    now = clock.now()

    async with db.transaction() as s:
        inserted = (
            await s.execute(
                insert(table)
                .values(
                    scope=scope,
                    key=key,
                    fingerprint=fingerprint,
                    state="IN_PROGRESS",
                    created_at=now,
                )
                .on_conflict_do_nothing()
                .returning(table.c.key)
            )
        ).first()
        existing = None
        if inserted is None:
            existing = (
                await s.execute(
                    sa.select(table)
                    .where(table.c.scope == scope, table.c.key == key)
                    .with_for_update()
                )
            ).first()
            expired = existing.created_at < now - RETENTION
            abandoned = existing.state == "IN_PROGRESS" and existing.created_at < now - (
                ABANDONED_AFTER
            )
            if expired or abandoned:
                # Take the key over as a fresh request.
                await s.execute(
                    table.update()
                    .where(table.c.scope == scope, table.c.key == key)
                    .values(
                        fingerprint=fingerprint,
                        state="IN_PROGRESS",
                        response_status=None,
                        response_body=None,
                        created_at=now,
                    )
                )
                existing = None

    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise AppError(
                422, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for another request"
            )
        if existing.state == "IN_PROGRESS":
            raise AppError(
                409, "IDEMPOTENCY_IN_PROGRESS", "A request with this Idempotency-Key is running"
            )
        return Response(
            content=existing.response_body,
            status_code=existing.response_status,
            media_type="application/json",
            headers={"Idempotent-Replayed": "true"},
        )

    try:
        status, body = await handler()
        content = jsonable_encoder(body)
    except AppError as exc:
        if exc.status >= 500:
            await _forget(db, table, scope, key)
            raise
        status = exc.status
        content = ErrorBody(
            error_code=exc.error_code,
            message=exc.message,
            correlation_id=correlation.get(),
            details=exc.details,
        ).model_dump(exclude_none=True)
    except BaseException:
        await _forget(db, table, scope, key)
        raise

    raw_body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    async with db.transaction() as s:
        await s.execute(
            table.update()
            .where(table.c.scope == scope, table.c.key == key)
            .values(state="DONE", response_status=status, response_body=raw_body)
        )
    return Response(content=raw_body, status_code=status, media_type="application/json")


async def _forget(db: Database, table: sa.Table, scope: str, key: str) -> None:
    async with db.transaction() as s:
        await s.execute(table.delete().where(table.c.scope == scope, table.c.key == key))
