"""Root fixtures for every test.

Order of a run:

1. ``pytest_sessionstart`` (controller only): wait for every service's ``/ready``, then
   reset and reseed the environment once.
2. Each test asks for what it needs by name: ``services`` (HTTP clients), ``controls``
   (the /test/* levers), ``make_customer`` (a fresh customer), ``alice`` (the seed customer,
   read-only), ``shared_clock`` (move time, restored afterwards).
3. When a test fails, every request it sent is printed under the failure, each with its
   correlation ID.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from cardvault_tests import environment
from cardvault_tests.checks import expect, expect_status
from cardvault_tests.clients.control import EnrollmentControl, ServiceControl
from cardvault_tests.customers import Customer, Services, log_in
from cardvault_tests.data import ALICE
from cardvault_tests.http import exchanges, make_client
from cardvault_tests.models import NewCustomer
from cardvault_tests.settings import Settings, get_settings

# ---------------------------------------------------------------- run start


def _controls(settings: Settings, record: bool = True) -> dict[str, ServiceControl]:
    out: dict[str, ServiceControl] = {}
    for name, url in settings.services.items():
        cls = EnrollmentControl if name == "enrollment" else ServiceControl
        http = make_client(url, settings.http_timeout_seconds, record)
        out[name] = cls(name, http, settings.test_key)
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    if hasattr(session.config, "workerinput"):
        return  # an xdist worker: the controller has already prepared the environment
    if session.config.option.collectonly:
        return
    settings = get_settings()
    controls = _controls(settings, record=False)
    environment.wait_until_ready(controls, settings.ready_wait_seconds)
    if settings.reset_at_start:
        environment.reset_and_seed(controls)


def pytest_report_header(config: pytest.Config) -> str:
    s = get_settings()
    return f"CardVault environment: {s.test_env} (auth at {s.auth_url})"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Tests that change something every service shares run in one xdist group, one at a time.
    for item in items:
        if item.get_closest_marker("serial"):
            item.add_marker(pytest.mark.xdist_group("serial"))


# ---------------------------------------------------------------- evidence


@pytest.fixture(autouse=True)
def _exchange_log() -> Iterator[None]:
    exchanges.clear()
    yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    if report.failed and exchanges.items:
        report.sections.append(("CardVault requests", exchanges.report()))
    return report


# ---------------------------------------------------------------- clients


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def services(settings: Settings) -> Iterator[Services]:
    t = settings.http_timeout_seconds
    s = Services(
        auth=make_client(settings.auth_url, t),
        card=make_client(settings.card_url, t),
        enrollment=make_client(settings.enrollment_url, t),
        wallet_txn=make_client(settings.wallet_txn_url, t),
        profile=make_client(settings.profile_url, t),
        operator=make_client(settings.operator_url, t),
        network=make_client(settings.network_url, t),
    )
    yield s
    for client in vars(s).values():
        client.close()


@pytest.fixture(scope="session")
def controls(settings: Settings) -> dict[str, ServiceControl]:
    return _controls(settings)


@pytest.fixture(scope="session")
def orchestrator(controls: dict[str, ServiceControl]) -> EnrollmentControl:
    control = controls["enrollment"]
    assert isinstance(control, EnrollmentControl)
    return control


# ---------------------------------------------------------------- customers


@pytest.fixture
def make_customer(
    services: Services, orchestrator: EnrollmentControl
) -> Callable[[dict[str, Any]], Customer]:
    """Create a fresh customer from a template (see ``data.customer``) and log in."""

    def make(template: dict[str, Any]) -> Customer:
        created = expect(orchestrator.create_customer(template), 201, NewCustomer)
        return log_in(services, created.username, created.password, created.cards)

    return make


@pytest.fixture(scope="session")
def alice(services: Services, settings: Settings) -> Customer:
    """The seed customer alice, logged in. Read-only: never change her data."""
    return log_in(services, ALICE.username, settings.seed_user_password, [])


# ---------------------------------------------------------------- shared state


@pytest.fixture
def shared_clock(controls: dict[str, ServiceControl]) -> Iterator[ServiceControl]:
    """Move CardVault's shared clock in a ``serial`` test; real time is restored afterwards,
    whether the test passed or failed."""
    control = controls["auth"]
    yield control
    expect_status(control.set_clock(reset=True), 200)
