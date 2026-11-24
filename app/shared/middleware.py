"""Request context: correlation ID, access log line and a last-resort 500 handler.

Written as plain ASGI middleware (not ``BaseHTTPMiddleware``) so the context variable set
here is the one seen by the handler, and streaming responses are not buffered.
"""

from __future__ import annotations

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shared import correlation
from shared.errors import error_response

log = logging.getLogger("access")

_QUIET_PATHS = {"/health", "/ready"}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for name, value in scope["headers"]:
            if name == b"x-correlation-id":
                raw = value.decode("latin-1")
                break
        cid = correlation.accept(raw)
        token = correlation.set(cid)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)[correlation.HEADER] = cid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error")
            if response_started:
                raise
            response = error_response(500, "INTERNAL_ERROR", "Unexpected server error")
            await response(scope, receive, send_wrapper)
        finally:
            path = scope["path"]
            if path not in _QUIET_PATHS:
                log.info(
                    "%s %s %s",
                    scope["method"],
                    path,
                    status,
                    extra={
                        "method": scope["method"],
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            correlation.reset(token)
