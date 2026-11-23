"""Wallet eligibility: one pure function, so it can be unit-tested without a database.

A card is eligible when (spec 6.3 and 16.3):

1. it is ACTIVE                                        else ``CARD_NOT_ACTIVE``
2. it has not expired (expires 00:00 UTC on the first
   day of the month after its expiry month)            else ``CARD_EXPIRED``
3. it is CREDIT or DEBIT (not PREPAID)                 else ``PRODUCT_NOT_SUPPORTED``
4. ``now - last_used_at <= 90 days``; never used fails  else ``NOT_RECENTLY_USED``
5. the customer has not opted out                      else ``CUSTOMER_OPTED_OUT``
6. the customer has not removed this card              else ``CARD_REMOVED_BY_CUSTOMER``
7. the customer's email is verified                    else ``EMAIL_NOT_VERIFIED``
8. the customer's phone is verified                    else ``PHONE_NOT_VERIFIED``

The first failing rule is the reason shown. ``now`` is always the shared clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

RECENT_USE = timedelta(days=90)
SUPPORTED_PRODUCTS = frozenset({"CREDIT", "DEBIT"})

REASONS = (
    "CARD_NOT_ACTIVE",
    "CARD_EXPIRED",
    "PRODUCT_NOT_SUPPORTED",
    "NOT_RECENTLY_USED",
    "CUSTOMER_OPTED_OUT",
    "CARD_REMOVED_BY_CUSTOMER",
    "EMAIL_NOT_VERIFIED",
    "PHONE_NOT_VERIFIED",
)


class CardFacts(Protocol):
    status: str
    product: str
    expiry: str  # YYYY-MM
    last_used_at: datetime | None


class ContactFacts(Protocol):
    email_verified: bool
    phone_verified: bool


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str | None = None


ELIGIBLE = Eligibility(True)


def expires_at(expiry: str) -> datetime:
    year, month = (int(p) for p in expiry.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def evaluate(
    card: CardFacts,
    contact: ContactFacts | None,
    *,
    opted_out: bool,
    removed_by_customer: bool,
    now: datetime,
) -> Eligibility:
    if card.status != "ACTIVE":
        return Eligibility(False, "CARD_NOT_ACTIVE")
    if now >= expires_at(card.expiry):
        return Eligibility(False, "CARD_EXPIRED")
    if card.product not in SUPPORTED_PRODUCTS:
        return Eligibility(False, "PRODUCT_NOT_SUPPORTED")
    if card.last_used_at is None or now - card.last_used_at > RECENT_USE:
        return Eligibility(False, "NOT_RECENTLY_USED")
    if opted_out:
        return Eligibility(False, "CUSTOMER_OPTED_OUT")
    if removed_by_customer:
        return Eligibility(False, "CARD_REMOVED_BY_CUSTOMER")
    if contact is None or not contact.email_verified:
        return Eligibility(False, "EMAIL_NOT_VERIFIED")
    if not contact.phone_verified:
        return Eligibility(False, "PHONE_NOT_VERIFIED")
    return ELIGIBLE
