"""One error shape everywhere: ``{error_code, message, correlation_id, details?}``.

Every ``error_code`` is listed with its HTTP status in ``docs/errors.md``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared import correlation

log = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    """The common error response body."""

    error_code: str
    message: str
    correlation_id: str | None
    details: Any | None = None


class AppError(Exception):
    """Raise anywhere in a handler to return the common error shape."""

    def __init__(
        self,
        status: int,
        error_code: str,
        message: str,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message
        self.details = details
        self.headers = headers


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def error_response(
    status: int,
    error_code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorBody(
        error_code=error_code,
        message=message,
        correlation_id=correlation.get(),
        details=details,
    ).model_dump(exclude_none=True)
    body.setdefault("correlation_id", None)
    return JSONResponse(status_code=status, content=body, headers=headers)


def _clean_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    # Drop ``input`` and ``ctx``: they can echo secrets (passwords) back to the caller.
    return [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]


def install(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status, exc.error_code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "VALIDATION_ERROR", "Request validation failed", _clean_validation_errors(exc)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ").title()
        return error_response(exc.status_code, code, message, headers=exc.headers)
