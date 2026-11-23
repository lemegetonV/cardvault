"""The shared clock. Every service and mock gets "now" only from ``clock.now()``.

* Production mode: real UTC.
* TEST_MODE: real UTC plus an offset stored in one row of the shared ``testsupport``
  database. A background task re-reads the row every 0.5 s, so the cached offset is never
  more than 1 s old. ``set_offset`` writes the row and updates the local cache at once,
  so the calling service sees the new time before ``POST /test/clock`` returns (rule 44).

``now()`` stays synchronous and cheap (no I/O per call), which matters because eligibility,
expiry, JWT and callback checks call it on hot paths.

The clock drives business rules only. Relay backoff, mock delays, consumer retries and UI
polling use real time (rule 43).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 0.5

_offset_seconds: float = 0.0
_fetched_at: float = 0.0  # time.monotonic() of the last successful read
_engine: AsyncEngine | None = None
_task: asyncio.Task | None = None


def real_now() -> datetime:
    return datetime.now(UTC)


def now() -> datetime:
    """The effective business time (UTC, timezone-aware)."""
    if _offset_seconds == 0.0:
        return real_now()
    return real_now() + timedelta(seconds=_offset_seconds)


def offset_seconds() -> float:
    return _offset_seconds


def cache_age_seconds() -> float | None:
    return None if _engine is None else time.monotonic() - _fetched_at


async def _read_offset() -> float:
    assert _engine is not None
    async with _engine.connect() as conn:
        value = await conn.scalar(text("SELECT offset_seconds FROM clock_offset WHERE id = 1"))
    return float(value or 0.0)


async def refresh() -> None:
    global _offset_seconds, _fetched_at
    _offset_seconds = await _read_offset()
    _fetched_at = time.monotonic()


async def _refresh_loop() -> None:
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the last known offset; the next tick tries again.
            log.warning("clock refresh failed", exc_info=True)
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def start(testsupport_database_url: str | None, test_mode: bool) -> None:
    """Start following the shared offset. Outside TEST_MODE the clock is real time."""
    global _engine, _task
    if not test_mode or not testsupport_database_url:
        return
    _engine = create_async_engine(testsupport_database_url, pool_size=2, max_overflow=2)
    await refresh()
    _task = asyncio.create_task(_refresh_loop(), name="clock-refresh")


async def stop() -> None:
    global _engine, _task
    if _task is not None:
        _task.cancel()
        _task = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def set_offset(seconds: float) -> None:
    """Write the single offset row, then update this process's cache immediately."""
    global _offset_seconds, _fetched_at
    if _engine is None:
        raise RuntimeError("the shared clock can only be moved in TEST_MODE")
    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO clock_offset (id, offset_seconds, updated_at) "
                "VALUES (1, :s, now()) "
                "ON CONFLICT (id) DO UPDATE SET offset_seconds = :s, updated_at = now()"
            ),
            {"s": seconds},
        )
    _offset_seconds = seconds
    _fetched_at = time.monotonic()
