"""Runtime fault injection (spec 9.1, 16.7 rule 48), controlled by ``/test/faults``.

Fault types:

* ``latency``: add ``latency_ms`` before handling a request.
* ``error``: answer with ``status`` (default 503) instead of handling the request.
* ``timeout``: hang for ``timeout_ms`` (default 30 s), then answer 504.
* ``kafka_publish_failure``: the Kafka producer raises instead of publishing.
* ``consumer_pause``: Kafka consumers stop fetching.
* ``relay_pause``: outbox relays stop draining.

Each fault can be limited by ``scope`` and made deterministic:

* ``fail_next: N``: fire on the next N matching operations, then expire.
* ``rate`` + ``seed``: fire with that probability using a seeded RNG, so a run repeats.
* neither: fire on every matching operation until reset.

``scope`` is ``{"method": "POST", "path": "/v1/auth/login"}`` for HTTP faults (a path
ending in ``*`` is a prefix) or ``{"topic": "card.lifecycle"}`` for Kafka faults. A missing
key matches everything.
"""

from __future__ import annotations

import asyncio
import random
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from shared.errors import error_response

FaultType = Literal[
    "latency", "error", "timeout", "kafka_publish_failure", "consumer_pause", "relay_pause"
]
HTTP_FAULTS = {"latency", "error", "timeout"}


class FaultScope(BaseModel):
    method: str | None = Field(None, description="HTTP method to match, e.g. POST")
    path: str | None = Field(None, description="Exact path, or a prefix ending in *")
    topic: str | None = Field(None, description="Kafka topic, for Kafka faults")


class FaultSpec(BaseModel):
    """One fault. See the module docs for the meaning of each field."""

    type: FaultType
    scope: FaultScope = Field(default_factory=FaultScope)
    fail_next: int | None = Field(None, ge=1, description="Fire on the next N matches only")
    rate: float | None = Field(None, gt=0, le=1, description="Probability of firing per match")
    seed: int | None = Field(None, description="RNG seed for rate mode (deterministic runs)")
    latency_ms: int = Field(0, ge=0, le=120_000)
    status: int = Field(503, ge=500, le=599, description="Status for the error fault")
    timeout_ms: int = Field(30_000, ge=0, le=300_000)

    @model_validator(mode="after")
    def _one_mode(self) -> FaultSpec:
        if self.fail_next is not None and self.rate is not None:
            raise ValueError("use either fail_next or rate, not both")
        return self


class _Active:
    def __init__(self, spec: FaultSpec) -> None:
        self.spec = spec
        self.remaining = spec.fail_next
        self.rng = random.Random(spec.seed)
        self.fired = 0

    def matches(self, method: str | None, path: str | None, topic: str | None) -> bool:
        s = self.spec.scope
        if s.method and method and s.method.upper() != method.upper():
            return False
        if s.path and path is not None:
            if s.path.endswith("*"):
                if not path.startswith(s.path[:-1]):
                    return False
            elif s.path != path:
                return False
        return not (s.topic and topic is not None and s.topic != topic)

    def expired(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def fire(self) -> bool:
        if self.expired():
            return False
        if self.remaining is not None:
            self.remaining -= 1
        elif self.spec.rate is not None and self.rng.random() >= self.spec.rate:
            return False
        self.fired += 1
        return True

    def view(self) -> dict:
        data = self.spec.model_dump(exclude_none=True)
        data["remaining"] = self.remaining
        data["fired"] = self.fired
        return data


class FaultRegistry:
    def __init__(self) -> None:
        self._faults: list[_Active] = []

    def replace(self, specs: list[FaultSpec]) -> None:
        self._faults = [_Active(s) for s in specs]

    def clear(self) -> None:
        self._faults = []

    def list(self) -> list[dict]:
        self._faults = [f for f in self._faults if not f.expired()]
        return [f.view() for f in self._faults]

    def take(
        self,
        fault_type: str,
        *,
        method: str | None = None,
        path: str | None = None,
        topic: str | None = None,
    ) -> FaultSpec | None:
        """Return the spec if a fault of this type fires for this operation (consumes it)."""
        for fault in self._faults:
            if (
                fault.spec.type == fault_type
                and fault.matches(method, path, topic)
                and fault.fire()
            ):
                return fault.spec
        return None

    def is_active(self, fault_type: str, *, topic: str | None = None) -> bool:
        """For pause faults: true while a matching, unexpired fault exists (not consumed)."""
        return any(
            f.spec.type == fault_type and not f.expired() and f.matches(None, None, topic)
            for f in self._faults
        )


registry = FaultRegistry()

# Paths never subject to HTTP faults, so tests can always reach the controls and probes.
_EXEMPT_PREFIXES = ("/test/", "/health", "/ready", "/docs", "/openapi.json")


class FaultMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        if spec := registry.take("latency", method=method, path=path):
            await asyncio.sleep(spec.latency_ms / 1000)
        if spec := registry.take("timeout", method=method, path=path):
            await asyncio.sleep(spec.timeout_ms / 1000)
            response = error_response(504, "FAULT_TIMEOUT", "Injected timeout")
            await response(scope, receive, send)
            return
        if spec := registry.take("error", method=method, path=path):
            response = error_response(spec.status, "FAULT_INJECTED", "Injected fault")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
