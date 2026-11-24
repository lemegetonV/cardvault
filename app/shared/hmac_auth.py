"""HMAC-SHA256 signing for callbacks from the wallet operator (spec 16.6 rule 36).

* ``X-Timestamp``: Unix seconds.
* Signed string: ``"{timestamp}.{raw_body}"`` (bytes).
* ``X-Signature``: lowercase hex HMAC-SHA256 of that string.
* A timestamp more than 300 s in the past or the future is rejected; exactly 300 s passes.

The timestamp window is measured on the shared clock, so tests can move time to produce
stale callbacks without sleeping.
"""

from __future__ import annotations

import hashlib
import hmac

from shared import clock
from shared.errors import AppError

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
MAX_SKEW_SECONDS = 300


def sign(secret: str | bytes, timestamp: int, body: bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    message = f"{timestamp}.".encode() + body
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def signature_headers(secret: str | bytes, body: bytes, timestamp: int | None = None) -> dict:
    """Headers for an outgoing signed request. The timestamp comes from the shared clock."""
    ts = int(clock.now().timestamp()) if timestamp is None else timestamp
    return {TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign(secret, ts, body)}


def verify(
    secret: str | bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    *,
    check_timestamp: bool = True,
) -> int:
    """Validate a signed request. Returns the timestamp; raises a 401 ``AppError`` otherwise.

    The signature is checked before the timestamp window, so a forged request never learns
    whether its timestamp would have been accepted.
    """
    if not timestamp_header or not signature_header:
        raise AppError(401, "SIGNATURE_MISSING", "X-Signature and X-Timestamp are required")
    try:
        ts = int(timestamp_header)
    except ValueError:
        raise AppError(401, "SIGNATURE_INVALID", "X-Timestamp must be Unix seconds") from None
    expected = sign(secret, ts, body)
    if not hmac.compare_digest(expected, signature_header):
        raise AppError(401, "SIGNATURE_INVALID", "Signature does not match")
    if check_timestamp:
        skew = abs(int(clock.now().timestamp()) - ts)
        if skew > MAX_SKEW_SECONDS:
            raise AppError(
                401,
                "SIGNATURE_EXPIRED",
                "X-Timestamp is outside the 5 minute window",
                {"skew_seconds": skew, "max_skew_seconds": MAX_SKEW_SECONDS},
            )
    return ts
