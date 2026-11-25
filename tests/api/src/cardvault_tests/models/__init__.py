"""Pydantic models of CardVault's responses.

Validating a response against its model checks its shape (fields present, right types,
allowed values) in one line, so tests can assert on business values only. Models allow
extra fields, because a new field is not a breaking change for a client.
"""

from cardvault_tests.models.auth import Me, Tokens
from cardvault_tests.models.cards import Card, CardPage
from cardvault_tests.models.common import ErrorBody
from cardvault_tests.models.payments import Authorization, Clearing
from cardvault_tests.models.testsupport import ClockView, NewCard, NewCustomer

__all__ = [
    "Authorization",
    "Clearing",
    "Card",
    "CardPage",
    "ClockView",
    "ErrorBody",
    "Me",
    "NewCard",
    "NewCustomer",
    "Tokens",
]
