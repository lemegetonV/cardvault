"""Transactional outbox and relay (spec section 5, 16.5 rule 31).

A service writes its business rows and an ``outbox`` row in the same transaction, so an
event (or an outbound call to the wallet operator) exists if and only if the commit
succeeds. A background relay drains the table:

* Head-of-line per key: only the oldest pending row of each ``key`` is eligible, so events
  for one card are delivered in commit order even across retries.
* Rows are locked with ``FOR UPDATE SKIP LOCKED``, so a second relay would not double-send.
* Delivery is at-least-once: a crash between "sent" and "marked sent" re-sends the row.
  Consumers dedupe by ``event_id``.
* Failures back off in real time (base, factor, cap from env). Event rows retry forever;
  rows of a kind with ``max_attempts`` set are marked failed and handed to ``on_give_up``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from shared import correlation, events
from shared.db import Database
from shared.faults import registry as faults
from shared.settings import BackoffSettings

log = logging.getLogger(__name__)

KIND_EVENT = "event"


def _columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("headers", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
    ]


def outbox_table(metadata: sa.MetaData, name: str = "outbox") -> sa.Table:
    return sa.Table(name, metadata, *_columns())


def create_outbox_table(op, name: str = "outbox") -> None:
    """Alembic helper: every service's first migration creates the same outbox table."""
    op.create_table(name, *_columns())
    op.create_index(
        f"ix_{name}_pending",
        name,
        ["key", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


async def enqueue_event(
    session: AsyncSession, table: sa.Table, topic: str, key: str, envelope: events.Envelope
) -> None:
    """Add an event to the outbox inside the caller's transaction."""
    await session.execute(
        table.insert().values(
            kind=KIND_EVENT,
            topic=topic,
            key=key,
            payload=envelope.model_dump(mode="json"),
            headers={"correlation_id": envelope.correlation_id or correlation.get_or_new()},
        )
    )


async def enqueue(
    session: AsyncSession,
    table: sa.Table,
    *,
    kind: str,
    topic: str,
    key: str,
    payload: dict[str, Any],
) -> None:
    """Add a non-event row (for example an outbound HTTP call) to the outbox."""
    await session.execute(
        table.insert().values(
            kind=kind,
            topic=topic,
            key=key,
            payload=payload,
            headers={"correlation_id": correlation.get_or_new()},
        )
    )


@dataclass
class OutboxRow:
    id: int
    kind: str
    topic: str
    key: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    attempts: int

    @classmethod
    def from_row(cls, row) -> OutboxRow:
        m = row._mapping
        return cls(
            id=m["id"],
            kind=m["kind"],
            topic=m["topic"],
            key=m["key"],
            payload=m["payload"],
            headers=m["headers"] or {},
            attempts=m["attempts"],
        )


DeliverFn = Callable[[OutboxRow], Awaitable[None]]
GiveUpFn = Callable[[AsyncSession, OutboxRow, str], Awaitable[None]]


@dataclass
class KindHandler:
    deliver: DeliverFn
    backoff: BackoffSettings
    on_give_up: GiveUpFn | None = None  # called in the same transaction that marks it failed


class OutboxRelay:
    def __init__(
        self,
        db: Database,
        table: sa.Table,
        handlers: dict[str, KindHandler],
        poll_interval: float = 0.25,
        batch_size: int = 100,
    ) -> None:
        self.db = db
        self.table = table
        self.handlers = handlers
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._wake = asyncio.Event()
        self._held = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"relay-{self.table.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def wake(self) -> None:
        """Call after a commit that wrote outbox rows, to skip the poll wait."""
        self._wake.set()

    async def hold(self) -> None:
        self._held.set()
        await self._idle.wait()

    def release(self) -> None:
        self._held.clear()
        self.wake()

    async def depth(self) -> int:
        async with self.db.session() as s:
            q = (
                sa.select(sa.func.count())
                .select_from(self.table)
                .where(self.table.c.published_at.is_(None), self.table.c.failed_at.is_(None))
            )
            return int(await s.scalar(q) or 0)

    async def _run(self) -> None:
        while True:
            try:
                if self._held.is_set() or faults.is_active("relay_pause"):
                    await asyncio.sleep(self.poll_interval)
                    continue
                self._idle.clear()
                try:
                    drained = await self.drain_once()
                finally:
                    self._idle.set()
                if drained == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                    self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox relay loop error")
                await asyncio.sleep(1)

    async def drain_once(self) -> int:
        t = self.table
        heads = (
            sa.select(t.c.id)
            .where(t.c.published_at.is_(None), t.c.failed_at.is_(None))
            .order_by(t.c.key, t.c.id)
            .distinct(t.c.key)
        )
        query = (
            sa.select(t)
            .where(t.c.id.in_(heads), t.c.next_attempt_at <= sa.func.now())
            .order_by(t.c.id)
            .limit(self.batch_size)
            .with_for_update(skip_locked=True)
        )
        done = 0
        async with self.db.transaction() as session:
            rows = [OutboxRow.from_row(r) for r in await session.execute(query)]
            for row in rows:
                await self._deliver(session, row)
                done += 1
        return done

    async def _deliver(self, session: AsyncSession, row: OutboxRow) -> None:
        t = self.table
        handler = self.handlers.get(row.kind)
        token = correlation.set(row.headers.get("correlation_id") or correlation.new_id())
        try:
            if handler is None:
                raise RuntimeError(f"no outbox handler for kind {row.kind}")
            await handler.deliver(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts = row.attempts + 1
            error = f"{type(exc).__name__}: {exc}"[:1000]
            give_up = handler is not None and 0 < handler.backoff.max_attempts <= attempts
            if give_up:
                log.error("outbox row failed permanently", extra={"outbox_id": row.id})
                await session.execute(
                    t.update()
                    .where(t.c.id == row.id)
                    .values(attempts=attempts, failed_at=sa.func.now(), last_error=error)
                )
                if handler.on_give_up:
                    await handler.on_give_up(session, row, error)
            else:
                delay = handler.backoff.delay(attempts) if handler else 5.0
                log.warning(
                    "outbox delivery failed, will retry",
                    extra={"outbox_id": row.id, "attempts": attempts, "retry_in_s": delay},
                )
                await session.execute(
                    t.update()
                    .where(t.c.id == row.id)
                    .values(
                        attempts=attempts,
                        last_error=error,
                        next_attempt_at=sa.func.now() + timedelta(seconds=delay),
                    )
                )
        else:
            await session.execute(
                t.update()
                .where(t.c.id == row.id)
                .values(attempts=row.attempts + 1, published_at=sa.func.now())
            )
        finally:
            correlation.reset(token)


def kafka_event_handler(producer, backoff: BackoffSettings) -> KindHandler:
    """The standard handler for ``kind=event`` rows: publish the envelope to its topic."""

    async def deliver(row: OutboxRow) -> None:
        await producer.send_envelope(row.topic, row.key, row.payload)

    return KindHandler(deliver=deliver, backoff=backoff)
