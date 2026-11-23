"""Deterministic seed cards and wallet enrolments (spec section 10, 16.10 rule 64).

card-service, network-mock and (from phase 3) enrollment-service and the operator mock
all seed from these constants, so every database and mock agrees on IDs and tokens.
Amounts are integer cents. ``used_days_ago`` is relative to the shared clock at seed time.
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


def make_pan(rng: random.Random) -> str:
    body = PAN_PREFIX + "".join(str(rng.randrange(10)) for _ in range(9))
    return body + luhn_check_digit(body)


def seed_pans() -> dict[uuid.UUID, str]:
    """One deterministic, Luhn-valid PAN per seed card."""
    rng = random.Random(seed_ids.SEED)
    return {card.card_id: make_pan(rng) for card in CARDS}
