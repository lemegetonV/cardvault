"""A customer the suite can act as: credentials, a live session and their clients.

Tests that change state work on a fresh customer created through ``POST /test/customers``,
so they never depend on, or disturb, another test's data. That is what makes the suite safe
to run in any order and in parallel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from cardvault_tests.auth import BearerAuth
from cardvault_tests.checks import expect
from cardvault_tests.clients.auth import AuthClient
from cardvault_tests.clients.cards import CardsClient
from cardvault_tests.models import NewCard, Tokens


@dataclass
class Services:
    """One configured httpx client per service and mock, shared by the whole run."""

    auth: httpx.Client
    card: httpx.Client
    enrollment: httpx.Client
    wallet_txn: httpx.Client
    profile: httpx.Client
    operator: httpx.Client
    network: httpx.Client


@dataclass
class Customer:
    username: str
    password: str
    customer_id: uuid.UUID
    tokens: Tokens
    bearer: BearerAuth
    cards: CardsClient
    auth: AuthClient
    new_cards: list[NewCard]

    @property
    def card_ids(self) -> list[uuid.UUID]:
        return [c.card_id for c in self.new_cards]


def log_in(services: Services, username: str, password: str, new_cards: list[NewCard]) -> Customer:
    auth_client = AuthClient(services.auth)
    tokens = expect(auth_client.login(username, password), 200, Tokens)
    bearer = BearerAuth(tokens.access_token)
    return Customer(
        username=username,
        password=password,
        customer_id=tokens.customer_id,
        tokens=tokens,
        bearer=bearer,
        cards=CardsClient(services.card, bearer),
        auth=AuthClient(services.auth, bearer),
        new_cards=new_cards,
    )
