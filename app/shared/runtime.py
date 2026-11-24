"""Per-process runtime: the DB, Kafka producer, relays and consumers a service owns.

``create_app`` wires the common pieces every service needs: logging, correlation IDs,
the error shape, fault injection, ``/health`` and ``/ready``, the shared clock, and (in
TEST_MODE only) the ``/test/*`` controls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from shared import clock, errors, faults, kafka
from shared import logging as cv_logging
from shared.db import Database
from shared.middleware import RequestContextMiddleware
from shared.outbox import OutboxRelay
from shared.settings import Settings, get_settings

log = logging.getLogger(__name__)

Hook = Callable[["Runtime"], Awaitable[None]]


@dataclass
class Runtime:
    settings: Settings
    db: Database | None = None
    producer: kafka.Producer | None = None
    relays: list[OutboxRelay] = field(default_factory=list)
    consumers: list[kafka.Consumer] = field(default_factory=list)
    uses_kafka: bool = False

    # Test-support hooks (TEST_MODE). ``reset_tables`` are truncated by /test/reset.
    reset_tables: list[str] = field(default_factory=list)
    on_reset: list[Hook] = field(default_factory=list)
    on_seed: list[Hook] = field(default_factory=list)

    async def hold_background(self) -> None:
        await asyncio.gather(*(r.hold() for r in self.relays), *(c.hold() for c in self.consumers))

    async def release_background(self) -> None:
        for c in self.consumers:
            c.release()
        for r in self.relays:
            r.release()


@dataclass
class ServiceSpec:
    """What a service plugs into the common app."""

    name: str
    title: str
    description: str
    version: str = "0.1.0"
    routers: list[APIRouter] = field(default_factory=list)
    test_routers: list[APIRouter] = field(default_factory=list)
    uses_db: bool = True
    uses_kafka: bool = False
    reset_tables: list[str] = field(default_factory=list)
    on_startup: list[Hook] = field(default_factory=list)  # after DB/Kafka are up
    on_shutdown: list[Hook] = field(default_factory=list)
    on_reset: list[Hook] = field(default_factory=list)
    on_seed: list[Hook] = field(default_factory=list)
    openapi_tags: list[dict] | None = None


def create_app(spec: ServiceSpec) -> FastAPI:
    settings = get_settings()
    cv_logging.setup(spec.name, settings.log_level)
    runtime = Runtime(
        settings=settings,
        uses_kafka=spec.uses_kafka,
        reset_tables=list(spec.reset_tables),
        on_reset=list(spec.on_reset),
        on_seed=list(spec.on_seed),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await clock.start(settings.testsupport_database_url, settings.test_mode)
        if spec.uses_db:
            if not settings.database_url:
                raise RuntimeError("DATABASE_URL is not set")
            runtime.db = Database(settings.database_url)
        if spec.uses_kafka:
            if not settings.kafka_bootstrap:
                raise RuntimeError("KAFKA_BOOTSTRAP is not set")
            await kafka.ensure_topics(settings.kafka_bootstrap)
            runtime.producer = kafka.Producer(settings.kafka_bootstrap)
            await runtime.producer.start()
        for hook in spec.on_startup:
            await hook(runtime)
        for relay in runtime.relays:
            relay.start()
        for consumer in runtime.consumers:
            await consumer.start()
        log.info(
            "service started",
            extra={"test_mode": settings.test_mode, "port": settings.port},
        )
        try:
            yield
        finally:
            for consumer in runtime.consumers:
                await consumer.stop()
            for relay in runtime.relays:
                await relay.stop()
            for hook in spec.on_shutdown:
                await hook(runtime)
            if runtime.producer:
                await runtime.producer.stop()
            if runtime.db:
                await runtime.db.dispose()
            await clock.stop()

    app = FastAPI(
        title=spec.title,
        description=spec.description,
        version=spec.version,
        lifespan=lifespan,
        openapi_tags=spec.openapi_tags,
    )
    app.state.runtime = runtime
    errors.install(app)
    # Order: the last added runs first. Context (correlation, access log) wraps faults.
    app.add_middleware(faults.FaultMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(_probes(runtime))
    for router in spec.routers:
        app.include_router(router)
    if settings.test_mode:
        from shared.testsupport import router as test_router

        app.include_router(test_router(runtime))
        for router in spec.test_routers:
            app.include_router(router)
    return app


def _probes(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health", summary="Liveness", description="200 while the process is up.")
    async def health() -> dict:
        return {"status": "ok", "service": runtime.settings.service_name}

    @router.get(
        "/ready",
        summary="Readiness",
        description="200 when the service's database (and Kafka, if it uses Kafka) answer.",
        responses={503: {"description": "A dependency is not reachable"}},
    )
    async def ready() -> JSONResponse:
        checks: dict[str, str] = {}
        if runtime.db is not None:
            checks["database"] = await _check(runtime.db.ping())
        if runtime.uses_kafka:
            checks["kafka"] = (
                await _check(runtime.producer.ping()) if runtime.producer else "not started"
            )
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ready" if ok else "not_ready",
                "service": runtime.settings.service_name,
                "checks": checks,
            },
        )

    return router


async def _check(coro: Awaitable[None]) -> str:
    try:
        await asyncio.wait_for(coro, timeout=3)
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__}"
