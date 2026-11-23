"""network-mock: issues wallet tokens, simulates purchases and clearing.

Not part of the system under test; it is a controllable stand-in for the card network.
State (token map, purchases) lives in memory: ``/test/seed`` restores the seed tokens and a
restart loses everything else, which is fine for a mock.

Every call to card-service carries ``X-Service-Key`` (network-mock's key) and the current
correlation ID.

Controls (``GET|POST /test/behaviour``), each with ``fail_next`` or ``rate`` + ``seed``:

* ``duplicate_authorization``: send the same authorization (same trace_id) twice; the
  response includes both answers.
* ``timeout_retry``: give up on the first attempt after a very short timeout, then retry
  with the same trace_id (like a network that timed out waiting for the issuer).
* ``clearing_before_auth_event``: settle immediately in the same call, so the clearing
  record reaches the bank right behind the authorization (to race the auth.decided event).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared import http_client, seed_data
from shared.errors import AppError, ErrorBody
from shared.faults import FaultRegistry, FaultScope
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_service, require_test_key

CARD_URL = os.environ.get("CARD_URL", "http://card:8002")
SERVICE_KEY = os.environ.get("SERVICE_KEY_NETWORK_MOCK", "")

ControlType = Literal["duplicate_authorization", "timeout_retry", "clearing_before_auth_event"]


class ControlSpec(BaseModel):
    """One control. Without fail_next or rate it applies to every purchase until reset."""

    type: ControlType
    scope: FaultScope = Field(default_factory=FaultScope)
    fail_next: int | None = Field(None, ge=1, description="Apply to the next N purchases only")
    rate: float | None = Field(None, gt=0, le=1, description="Probability per purchase")
    seed: int | None = Field(None, description="RNG seed for rate mode")


class State:
    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}  # token_ref -> {card_id, enrollment_id}
        self.purchases: dict[str, dict[str, Any]] = {}  # trace_id -> purchase details
        self.controls = FaultRegistry()

    def reset(self) -> None:
        self.tokens.clear()
        self.purchases.clear()
        self.controls.clear()


state = State()


def _card() -> httpx.AsyncClient:
    return http_client.client(CARD_URL, service_key=SERVICE_KEY, timeout=10.0)


def _passthrough(resp: httpx.Response) -> JSONResponse:
    try:
        content = resp.json()
    except ValueError:
        content = {"error_code": "UPSTREAM_ERROR", "message": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=content)


# ---- Token service (called by the wallet operator) -------------------------------------

token_router = APIRouter(prefix="/v1", tags=["tokens"])


class TokenRequest(BaseModel):
    card_id: uuid.UUID = Field(description="The bank's card_id (never a card number)")
    enrollment_id: uuid.UUID | None = Field(None, description="The bank's enrolment ID")


class TokenResponse(BaseModel):
    token_ref: str
    card_id: uuid.UUID


@token_router.post(
    "/tokens",
    summary="Issue a wallet token",
    description="Called by the wallet operator during enrolment. Maps a new token_ref to the "
    "card_id; purchases with that token are routed to that card.",
    status_code=201,
    response_model=TokenResponse,
    responses={401: {"model": ErrorBody}},
)
async def issue_token(
    body: TokenRequest, _: str = Depends(require_service("wallet-operator-mock", "tests"))
) -> TokenResponse:
    token_ref = "tok_" + uuid.uuid4().hex[:24]
    state.tokens[token_ref] = {"card_id": str(body.card_id), "enrollment_id": body.enrollment_id}
    return TokenResponse(token_ref=token_ref, card_id=body.card_id)


# ---- Purchase and clearing simulator (driven by tests) ---------------------------------

sim_router = APIRouter(
    prefix="/v1/test", tags=["simulator"], dependencies=[Depends(require_test_key)]
)


class PurchaseRequest(BaseModel):
    token_ref: str = Field(min_length=1, max_length=64, description="Wallet token to charge")
    amount: int = Field(description="Cents. Not validated here, so tests can send bad values")
    merchant_name: str = Field("CardVault Test Merchant", description="Untrusted free text")
    mcc: str = Field("5999", description="Merchant category code")
    currency: str = "USD"


class CardPurchaseRequest(BaseModel):
    card_id: uuid.UUID = Field(description="Card to charge directly (no wallet token)")
    amount: int
    merchant_name: str = "CardVault Test Merchant"
    mcc: str = "5999"
    currency: str = "USD"


class ClearingAmount(BaseModel):
    amount: int | None = Field(None, description="Cents. Default: the authorized amount")


class RefundAmount(BaseModel):
    amount: int = Field(description="Cents to refund")


async def _authorize(body: dict[str, Any]) -> JSONResponse:
    trace_id = body["trace_id"]
    state.purchases[trace_id] = {**body, "card_id": str(body["card_id"])}
    extra: dict[str, Any] = {}
    async with _card() as client:
        if state.controls.take("timeout_retry"):
            with contextlib.suppress(httpx.TimeoutException):
                await client.post("/internal/v1/authorizations", json=body, timeout=0.001)
            extra["retried_after_timeout"] = True
        resp = await client.post("/internal/v1/authorizations", json=body)
        if state.controls.take("duplicate_authorization"):
            dup = await client.post("/internal/v1/authorizations", json=body)
            extra["duplicate_response"] = dup.json()
        if resp.status_code == 200 and state.controls.take("clearing_before_auth_event"):
            settle = await _clear(client, trace_id, "SETTLE", body["amount"])
            extra["settlement"] = settle.json()
    out = _passthrough(resp)
    if resp.status_code == 200 and extra:
        content = {**resp.json(), **extra}
        return JSONResponse(status_code=200, content=content)
    return out


async def _clear(client: httpx.AsyncClient, trace_id: str, kind: str, amount: int):
    return await client.post(
        "/internal/v1/clearing",
        json={
            "clearing_id": "clr_" + uuid.uuid4().hex,
            "trace_id": trace_id,
            "type": kind,
            "amount": amount,
            "currency": "USD",
        },
    )


@sim_router.post(
    "/purchases",
    summary="Simulate a wallet purchase",
    description="Resolves the token to its card, sends an authorization with a new trace_id "
    "and token_requestor WALLET, and returns the bank's decision (plus trace_id).",
)
async def purchase(body: PurchaseRequest) -> JSONResponse:
    token = state.tokens.get(body.token_ref)
    if token is None:
        raise AppError(404, "TOKEN_UNKNOWN", "The network has no such token")
    return await _authorize(
        {
            "trace_id": "trc_" + uuid.uuid4().hex,
            "card_id": token["card_id"],
            "token_ref": body.token_ref,
            "token_requestor": "WALLET",
            "amount": body.amount,
            "currency": body.currency,
            "merchant_name": body.merchant_name,
            "mcc": body.mcc,
        }
    )


@sim_router.post(
    "/card-purchases",
    summary="Simulate a plain card purchase",
    description="Charges the card directly (token_requestor CARD, no token). This is how an "
    "unused card becomes recently used and so eligible for the wallet.",
)
async def card_purchase(body: CardPurchaseRequest) -> JSONResponse:
    return await _authorize(
        {
            "trace_id": "trc_" + uuid.uuid4().hex,
            "card_id": str(body.card_id),
            "token_ref": None,
            "token_requestor": "CARD",
            "amount": body.amount,
            "currency": body.currency,
            "merchant_name": body.merchant_name,
            "mcc": body.mcc,
        }
    )


TRACE = Path(description="trace_id returned by a purchase")


def _purchase(trace_id: str) -> dict[str, Any]:
    p = state.purchases.get(trace_id)
    if p is None:
        raise AppError(404, "TRACE_UNKNOWN", "The network has no purchase with this trace_id")
    return p


@sim_router.post("/purchases/{trace_id}/settle", summary="Settle a purchase (clearing)")
async def settle(body: ClearingAmount | None = None, trace_id: str = TRACE) -> JSONResponse:
    p = _purchase(trace_id)
    amount = body.amount if body and body.amount is not None else p["amount"]
    async with _card() as client:
        return _passthrough(await _clear(client, trace_id, "SETTLE", amount))


@sim_router.post("/purchases/{trace_id}/reverse", summary="Reverse all or part of a purchase")
async def reverse(body: ClearingAmount | None = None, trace_id: str = TRACE) -> JSONResponse:
    p = _purchase(trace_id)
    amount = body.amount if body and body.amount is not None else p["amount"]
    async with _card() as client:
        return _passthrough(await _clear(client, trace_id, "REVERSE", amount))


@sim_router.post("/purchases/{trace_id}/refund", summary="Refund part or all of a settled purchase")
async def refund(body: RefundAmount, trace_id: str = TRACE) -> JSONResponse:
    _purchase(trace_id)
    async with _card() as client:
        return _passthrough(await _clear(client, trace_id, "REFUND", body.amount))


# ---- Controls -------------------------------------------------------------------------

control_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


class BehaviourRequest(BaseModel):
    """Replaces every control. ``{"controls": []}`` turns them all off."""

    controls: list[ControlSpec] = Field(default_factory=list)


@control_router.get("/behaviour", summary="Read the active controls")
async def get_behaviour() -> dict:
    return {"controls": state.controls.list()}


@control_router.post("/behaviour", summary="Replace the active controls")
async def set_behaviour(body: BehaviourRequest) -> dict:
    state.controls.replace(body.controls)  # type: ignore[arg-type]
    return {"controls": state.controls.list()}


@control_router.get("/state", summary="Tokens and purchases the network knows")
async def get_state() -> dict:
    return {"tokens": state.tokens, "purchases": state.purchases}


async def on_reset(_: Runtime) -> None:
    state.reset()


async def on_seed(_: Runtime) -> None:
    for card in seed_data.CARDS:
        e = card.enrollment
        if e is not None and e.token_ref:
            state.tokens[e.token_ref] = {
                "card_id": str(card.card_id),
                "enrollment_id": str(e.enrollment_id),
            }


app = create_app(
    ServiceSpec(
        name="network-mock",
        title="CardVault network-mock",
        description=(
            "A controllable stand-in for the card network: issues wallet tokens, simulates "
            "wallet and card purchases (authorizations to card-service) and clearing records."
        ),
        routers=[token_router, sim_router, control_router],
        uses_db=False,
        uses_kafka=False,
        on_reset=[on_reset],
        on_seed=[on_seed],
    )
)
