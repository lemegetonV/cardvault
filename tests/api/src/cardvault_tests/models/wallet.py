from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from cardvault_tests.models.common import ApiModel

EnrolmentStatus = Literal["PENDING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]


class Enrolment(ApiModel):
    enrollment_id: uuid.UUID
    status: EnrolmentStatus
    version: int
    last4: str
    expiry: str
    status_reason: str | None
    failure_reason: str | None
    updated_at: datetime


class WalletCard(ApiModel):
    card_id: uuid.UUID
    product: Literal["CREDIT", "DEBIT", "PREPAID"]
    last4: str
    expiry: str
    card_status: Literal["ACTIVE", "LOST", "CLOSED"]
    eligible: bool
    ineligible_reason: str | None
    enrollment: Enrolment | None


class Wallet(ApiModel):
    customer_id: uuid.UUID
    opted_out: bool
    terms_version: str | None
    cards: list[WalletCard]


class CallbackAck(ApiModel):
    result: Literal[
        "APPLIED", "DUPLICATE", "STALE", "SUPERSEDED", "IGNORED_REMOVED", "FAILURE_RECORDED"
    ]
    enrollment_id: uuid.UUID
    status: str | None


class Profile(ApiModel):
    customer_id: str
    email: str | None
    email_verified: bool
    phone: str | None
    phone_verified: bool
    version: int
    updated_at: datetime
