"""Common ``/test/*`` controls, mounted only when ``TEST_MODE=true`` and guarded by
``X-Test-Key``.

* ``POST /test/reset``: hold relays and consumers, truncate this service's tables, move
  consumer offsets to the end, clear faults, then resume (rule 46).
* ``POST /test/seed``: reset, then load the deterministic seed data.
* ``GET|POST /test/faults``: read or replace the active fault set.
* ``GET|POST /test/clock``: read or move the shared clock (one row for every service).
* ``GET /test/quiet``: outbox depth and consumer lag, so tests wait for "nothing in flight".
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from shared import bugs, clock
from shared.faults import FaultSpec
from shared.faults import registry as faults
from shared.security import require_test_key


class FaultsRequest(BaseModel):
    """Replaces the whole active fault set. ``{"faults": []}`` clears every fault."""

    faults: list[FaultSpec] = Field(default_factory=list)


class ClockRequest(BaseModel):
    """Exactly one of the fields. The offset applies to every service at once."""

    offset_seconds: float | None = Field(None, description="Set the offset from real time")
    advance_seconds: float | None = Field(None, description="Move the clock by this much")
    now: datetime | None = Field(None, description="Set the effective time (timezone-aware)")
    reset: bool | None = Field(None, description="true clears the offset (real time)")

    @model_validator(mode="after")
    def _exactly_one(self) -> ClockRequest:
        given = [
            name
            for name in ("offset_seconds", "advance_seconds", "now", "reset")
            if getattr(self, name) is not None
        ]
        if len(given) != 1:
            raise ValueError("send exactly one of offset_seconds, advance_seconds, now, reset")
        if self.now is not None and self.now.tzinfo is None:
            raise ValueError("now must include a timezone, e.g. 2026-01-01T00:00:00Z")
        return self


class ClockView(BaseModel):
    now: datetime
    real_now: datetime
    offset_seconds: float
    service: str


def router(runtime) -> APIRouter:
    r = APIRouter(prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)])
    service = runtime.settings.service_name

    async def do_reset() -> None:
        await runtime.hold_background()
        try:
            if runtime.db is not None and runtime.reset_tables:
                await runtime.db.truncate(*runtime.reset_tables)
            for consumer in runtime.consumers:
                await consumer.seek_to_end_and_commit()
            for hook in runtime.on_reset:
                await hook(runtime)
            faults.clear()
        finally:
            await runtime.release_background()

    @r.post(
        "/reset",
        summary="Reset this service",
        description="Truncates this service's tables, clears faults, idempotency keys and "
        "lockouts, and skips consumers to the end of their topics.",
    )
    async def reset() -> dict:
        await do_reset()
        return {"status": "reset", "service": service}

    @r.post(
        "/seed",
        summary="Load seed data",
        description="Resets this service, then writes the deterministic seed data.",
    )
    async def seed() -> dict:
        await do_reset()
        await runtime.hold_background()
        try:
            for hook in runtime.on_seed:
                await hook(runtime)
        finally:
            await runtime.release_background()
        return {"status": "seeded", "service": service}

    @r.get("/faults", summary="List active faults")
    async def get_faults() -> dict:
        return {"service": service, "faults": faults.list()}

    @r.post("/faults", summary="Replace the active fault set")
    async def set_faults(body: FaultsRequest) -> dict:
        faults.replace(body.faults)
        return {"service": service, "faults": faults.list()}

    def view() -> ClockView:
        return ClockView(
            now=clock.now(),
            real_now=clock.real_now(),
            offset_seconds=clock.offset_seconds(),
            service=service,
        )

    @r.get("/clock", summary="Read the effective time", response_model=ClockView)
    async def get_clock() -> ClockView:
        return view()

    @r.post(
        "/clock",
        summary="Move the shared clock",
        description="Writes the single shared offset row. Returns once this service sees the "
        "new time; other services follow within 1 second.",
        response_model=ClockView,
    )
    async def set_clock(body: ClockRequest) -> ClockView:
        if body.reset:
            new = 0.0
        elif body.offset_seconds is not None:
            new = body.offset_seconds
        elif body.advance_seconds is not None:
            await clock.refresh()
            new = clock.offset_seconds() + body.advance_seconds
        else:
            assert body.now is not None
            new = (body.now - clock.real_now()).total_seconds()
        await clock.set_offset(new)
        return view()

    @r.get(
        "/quiet",
        summary="Is anything in flight?",
        description="Outbox depth and consumer lag. quiet=true when both are zero.",
    )
    async def quiet() -> dict:
        depth = sum([await relay.depth() for relay in runtime.relays])
        lag = sum([await consumer.lag() for consumer in runtime.consumers])
        return {
            "service": service,
            "outbox_depth": depth,
            "consumer_lag": lag,
            "quiet": depth == 0 and lag == 0,
        }

    @r.get("/bugs", summary="Seeded-bug flags that are on in this service")
    async def active_bugs() -> dict:
        return {"service": service, "active": bugs.active()}

    return r
