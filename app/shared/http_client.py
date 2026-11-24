"""Outgoing HTTP to other services and mocks, with the correlation ID always attached."""

from __future__ import annotations

import httpx

from shared import correlation


async def _add_correlation(request: httpx.Request) -> None:
    request.headers.setdefault(correlation.HEADER, correlation.get_or_new())


def client(
    base_url: str, *, service_key: str | None = None, timeout: float = 5.0
) -> httpx.AsyncClient:
    headers = {"X-Service-Key": service_key} if service_key else {}
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        event_hooks={"request": [_add_correlation]},
    )
