"""Seeded-bug flags. Every flag defaults to false; default behaviour is always correct.

``make bug-up BUG=<FLAG>`` starts the stack with exactly one flag set to ``true``.
The list below is the single source of truth; ``scripts/bugs.txt`` mirrors it for shell
tooling and CI checks that the two agree.
"""

from __future__ import annotations

import os

KNOWN = (
    # Enrolment
    "BUG_INELIGIBLE_CARD_ENROLLED",
    "BUG_SUSPEND_NOT_PUSHED",
    "BUG_REISSUE_NOT_PUSHED",
    "BUG_OPT_OUT_PARTIAL",
    "BUG_CALLBACK_SIGNATURE_IGNORED",
    "BUG_CALLBACK_TIMESTAMP_NOT_CHECKED",
    "BUG_STALE_CALLBACK_OVERWRITES",
    "BUG_DUPLICATE_CALLBACK_APPLIED_TWICE",
    # Authorization
    "BUG_AUTH_NOT_IDEMPOTENT",
    "BUG_TOKEN_STATUS_IGNORED",
    "BUG_HOLD_NOT_RELEASED_ON_REVERSAL",
    "BUG_SETTLE_USES_AUTH_AMOUNT",
    "BUG_FLOAT_MONEY",
    "BUG_EVENT_PUBLISHED_WITHOUT_COMMIT",
    # Transaction record
    "BUG_TXN_DUPLICATE_ON_REDELIVERY",
    "BUG_CLEARED_BEFORE_AUTH_LOST",
    "BUG_CSV_FORMULA_INJECTION",
    # Reconciliation
    "BUG_RECON_MISSES_STATUS_MISMATCH",
    # Security
    "BUG_CROSS_CUSTOMER_WALLET_READ",
    "BUG_PAN_IN_LOGS",
    "BUG_IDEMPOTENCY_KEY_NOT_SCOPED",
    # UI and disputes
    "BUG_UI_UNESCAPED_MERCHANT",
    "BUG_UI_PENDING_NEVER_REFRESHES",
    "BUG_UI_DOUBLE_SUBMIT",
    "BUG_UI_TIMEZONE_UTC",
    "BUG_DISPUTE_FILE_TYPE_BY_NAME",
)


def enabled(flag: str) -> bool:
    if flag not in KNOWN:
        raise KeyError(f"unknown bug flag {flag}")
    return os.environ.get(flag, "false").strip().lower() == "true"


def active() -> list[str]:
    return [flag for flag in KNOWN if enabled(flag)]
