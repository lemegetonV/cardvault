"""Correlation IDs: accepted from ``X-Correlation-ID`` or generated, then carried everywhere.

The ID lives in a context variable so log lines, outgoing HTTP calls, Kafka headers, event
envelopes and callbacks can all read it without passing it through every function.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

HEADER = "X-Correlation-ID"
_VALID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

_current: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_id() -> str:
    return str(uuid.uuid4())


def accept(raw: str | None) -> str:
    """Use the caller's ID when it is safe to log and echo; otherwise generate one."""
    if raw and _VALID.match(raw):
        return raw
    return new_id()


def get() -> str | None:
    return _current.get()


def get_or_new() -> str:
    """The current ID, or a fresh one for work that did not start from a request."""
    value = _current.get()
    if value is None:
        value = new_id()
        _current.set(value)
    return value


def set(value: str | None):
    return _current.set(value)


def reset(token) -> None:
    _current.reset(token)
