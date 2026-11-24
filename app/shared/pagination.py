"""Cursor pagination (spec 16.5 rule 34): newest first, ties broken by id.

The cursor is opaque to clients: base64url of ``{"t": <created_at>, "id": <id>}`` for the
last item of the previous page. An unreadable cursor returns 400 ``INVALID_CURSOR``.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from fastapi import Query

from shared.errors import AppError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def limit_param(
    limit: int = Query(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Page size, 1 to 100 (default 20)"
    ),
) -> int:
    return limit


def encode(created_at: datetime, item_id: Any) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": str(item_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        ts = datetime.fromisoformat(data["t"])
        if ts.tzinfo is None:
            raise ValueError("naive timestamp")
        return ts, str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise AppError(400, "INVALID_CURSOR", "The cursor is not valid") from None
