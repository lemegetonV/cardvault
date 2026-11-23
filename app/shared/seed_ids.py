"""Fixed identifiers for the deterministic seed data (spec section 10).

Every service seeds from the same constants, so IDs agree across databases.
"""

from __future__ import annotations

import uuid

ALICE = uuid.UUID("a11ce000-0000-4000-8000-000000000001")
BOB = uuid.UUID("b0b00000-0000-4000-8000-000000000002")
CHARLIE = uuid.UUID("c4a21e00-0000-4000-8000-000000000003")
DANA = uuid.UUID("da4a0000-0000-4000-8000-000000000004")

CUSTOMERS = {
    "alice": (ALICE, "Alice Archer"),
    "bob": (BOB, "Bob Baker"),
    "charlie": (CHARLIE, "Charlie Chen"),
    "dana": (DANA, "Dana Diaz"),
}

# Seeded RNG for any generated seed values (amounts, merchants), so runs repeat.
SEED = 20261123
