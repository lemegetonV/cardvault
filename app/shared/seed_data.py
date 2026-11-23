"""Deterministic seed cards and wallet enrolments (spec section 10, 16.10 rule 64).

card-service, network-mock, profile-service, enrollment-service and the operator mock all
seed from these constants, so every database and mock agrees on IDs, last4s and tokens.
Amounts are integer cents. ``used_days_ago`` is relative to the shared clock at seed time.

Each seed card has a fixed ``last4``; card-service generates a Luhn-valid number that ends
with it. Other services only ever see the ``last4`` (no full number leaves card-service).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from shared import seed_ids


@dataclass(frozen=True)
class SeedEnrollment:
    enrollment_id: uuid.UUID
    token_ref: str | None
    status: str  # ENROLLED | SUSPENDED | REMOVED
    version: int = 1


@dataclass(frozen=True)
class SeedCard:
    card_id: uuid.UUID
    customer_id: uuid.UUID
    product: str
    status: str
    used_days_ago: int | None
    limit: int
    available: int
    expiry: str  # YYYY-MM
    label: str
    enrollment: SeedEnrollment | None = None

    @property
    def last4(self) -> str:
        return LAST4[self.label]


def _u(value: str) -> uuid.UUID:
    return uuid.UUID(value)


CARDS: tuple[SeedCard, ...] = (
    # alice: 2 ENROLLED, 2 ineligible (unused 120 days; prepaid)
    SeedCard(
        _u("ca4d0000-a11c-4000-8000-000000000001"),
        seed_ids.ALICE,
        "CREDIT",
        "ACTIVE",
        2,
        500_000,
        500_000,
        "2029-06",
        "alice-credit",
        SeedEnrollment(
            _u("e4400000-a11c-4000-8000-000000000001"), "tok_a11ce0c4ed170000000001", "ENROLLED"
        ),
    ),
    SeedCard(
        _u("ca4d0000-a11c-4000-8000-000000000002"),
        seed_ids.ALICE,
        "DEBIT",
        "ACTIVE",
        5,
        250_000,
        250_000,
        "2028-11",
        "alice-debit",
        SeedEnrollment(
            _u("e4400000-a11c-4000-8000-000000000002"), "tok_a11ce0deb1700000000002", "ENROLLED"
        ),
    ),
    SeedCard(
        _u("ca4d0000-a11c-4000-8000-000000000003"),
        seed_ids.ALICE,
        "CREDIT",
        "ACTIVE",
        120,
        300_000,
        300_000,
        "2030-02",
        "alice-credit-unused",
    ),
    SeedCard(
        _u("ca4d0000-a11c-4000-8000-000000000004"),
        seed_ids.ALICE,
        "PREPAID",
        "ACTIVE",
        3,
        50_000,
        50_000,
        "2027-09",
        "alice-prepaid",
    ),
    # bob: 1 ENROLLED, 1 LOST (SUSPENDED)
    SeedCard(
        _u("ca4d0000-b0b0-4000-8000-000000000001"),
        seed_ids.BOB,
        "CREDIT",
        "ACTIVE",
        1,
        400_000,
        400_000,
        "2029-03",
        "bob-credit",
        SeedEnrollment(
            _u("e4400000-b0b0-4000-8000-000000000001"), "tok_b0b0c4ed1700000000001", "ENROLLED"
        ),
    ),
    SeedCard(
        _u("ca4d0000-b0b0-4000-8000-000000000002"),
        seed_ids.BOB,
        "DEBIT",
        "LOST",
        10,
        200_000,
        200_000,
        "2028-07",
        "bob-debit-lost",
        SeedEnrollment(
            _u("e4400000-b0b0-4000-8000-000000000002"),
            "tok_b0b0deb170000000000002",
            "SUSPENDED",
            version=2,
        ),
    ),
    # charlie: opted out; one eligible card whose enrolment is REMOVED
    SeedCard(
        _u("ca4d0000-c4a2-4000-8000-000000000001"),
        seed_ids.CHARLIE,
        "CREDIT",
        "ACTIVE",
        4,
        300_000,
        300_000,
        "2029-12",
        "charlie-credit",
        SeedEnrollment(
            _u("e4400000-c4a2-4000-8000-000000000001"),
            "tok_c4a21ec4ed170000000001",
            "REMOVED",
            version=2,
        ),
    ),
    # dana: phone not verified, so both cards are ineligible
    SeedCard(
        _u("ca4d0000-da4a-4000-8000-000000000001"),
        seed_ids.DANA,
        "CREDIT",
        "ACTIVE",
        2,
        300_000,
        300_000,
        "2029-01",
        "dana-credit",
    ),
    SeedCard(
        _u("ca4d0000-da4a-4000-8000-000000000002"),
        seed_ids.DANA,
        "DEBIT",
        "ACTIVE",
        6,
        150_000,
        150_000,
        "2028-05",
        "dana-debit",
    ),
)

BY_LABEL = {c.label: c for c in CARDS}

LAST4 = {
    "alice-credit": "4417",
    "alice-debit": "2045",
    "alice-credit-unused": "7380",
    "alice-prepaid": "5521",
    "bob-credit": "1029",
    "bob-debit-lost": "8836",
    "charlie-credit": "6604",
    "dana-credit": "3172",
    "dana-debit": "9958",
}

# Terms version every seed enrolment and opt-in was accepted under.
TERMS_VERSION = "2026-01"


@dataclass(frozen=True)
class SeedContact:
    email: str
    email_verified: bool
    phone: str
    phone_verified: bool
    version: int = 1


# example.com is reserved for documentation; +1 555-01xx numbers are fictional.
CONTACTS: dict[uuid.UUID, SeedContact] = {
    seed_ids.ALICE: SeedContact("alice@example.com", True, "+15550100101", True),
    seed_ids.BOB: SeedContact("bob@example.com", True, "+15550100102", True),
    seed_ids.CHARLIE: SeedContact("charlie@example.com", True, "+15550100103", True),
    seed_ids.DANA: SeedContact("dana@example.com", True, "+15550100104", False),
}

# Customers who opted out of the wallet (spec section 10).
OPTED_OUT = frozenset({seed_ids.CHARLIE})


def operator_ref(enrollment_id: uuid.UUID) -> str:
    """The operator mock's reference for an enrolment (seed data uses the same rule)."""
    return "op_" + enrollment_id.hex


PAN_PREFIX = "990000"  # fictional issuer prefix, not assigned to any real network


def luhn_check_digit(partial: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(partial)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def make_pan(rng: random.Random, last4: str | None = None) -> str:
    """A Luhn-valid 16-digit number with the fictional prefix, optionally ending in ``last4``.

    The last digit is the check digit, so for a fixed ``last4`` the middle digits are drawn
    again until the check digit comes out right (about 10 draws).
    """
    while True:
        middle = "".join(str(rng.randrange(10)) for _ in range(6 if last4 else 9))
        body = PAN_PREFIX + middle + (last4[:3] if last4 else "")
        pan = body + luhn_check_digit(body)
        if last4 is None or pan.endswith(last4):
            return pan


def seed_pans() -> dict[uuid.UUID, str]:
    """One deterministic, Luhn-valid PAN per seed card, ending in the card's fixed last4."""
    rng = random.Random(seed_ids.SEED)
    return {card.card_id: make_pan(rng, card.last4) for card in CARDS}
