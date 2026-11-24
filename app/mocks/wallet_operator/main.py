"""wallet-operator-mock: a controllable stand-in for the wallet operator.

Not part of the system under test. State lives in memory; ``/test/seed`` restores records
that match the bank's seed enrolments, and ``/test/reset`` cancels every pending callback.

* Requests from the bank are HMAC-signed (same scheme as the callbacks) and carry an
  ``Idempotency-Key``. A repeated key returns the original 202 and sends no second callback
  (unless the ``duplicate_callback`` control is on); the same key with a different body is
  409.
* Each accepted request returns 202 and, after a delay (``OPERATOR_CALLBACK_DELAY_MS``,
  default 2000, real time), sends a signed callback to the bank:
  ``{event_id, operator_ref, enrollment_id, status, token_ref?, reason?, operator_version,
  occurred_at}``. ``operator_version`` rises by one with every change to a record.
* Enrolment asks network-mock for a token. Re-enrolment of the same ``enrollment_id``
  reuses the record and its ``operator_ref`` and gets a new token.

Controls (``GET|POST /test/behaviour``), each with ``fail_next`` or ``rate`` + ``seed`` and
an optional ``scope`` (method, path):

* ``callback_delay`` (``delay_ms``), ``error`` (``status``, default 503, nothing applied),
  ``timeout`` (``timeout_ms``, then 504, nothing applied)
* ``duplicate_callback``: the callback is sent twice with the same event_id
* ``out_of_order_callback``: after the callback, the record's previous callback is sent
  again (older operator_version, new event_id)
* ``bad_signature``, ``drop_callback``
* ``decline_enrollment`` (``reason``): enrolment ends in a FAILED callback
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import logging
import os
import uuid
from datetime import date
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from shared import clock, hmac_auth, http_client, seed_data
from shared.errors import AppError, ErrorBody
from shared.faults import FaultRegistry, FaultScope
from shared.runtime import Runtime, ServiceSpec, create_app
from shared.security import require_test_key

log = logging.getLogger(__name__)

BANK_CALLBACK_URL = os.environ.get(
    "BANK_CALLBACK_URL", "http://enrollment:8003/v1/callbacks/operator"
)
NETWORK_URL = os.environ.get("NETWORK_URL", "http://network-mock:8102")
SECRET = os.environ.get("OPERATOR_HMAC_SECRET", "")
SERVICE_KEY = os.environ.get("SERVICE_KEY_OPERATOR_MOCK", "")
DEFAULT_DELAY_MS = int(os.environ.get("OPERATOR_CALLBACK_DELAY_MS", "2000"))
CALLBACK_ATTEMPTS = 3

ControlType = Literal[
    "callback_delay",
    "error",
    "timeout",
    "duplicate_callback",
    "out_of_order_callback",
    "bad_signature",
    "drop_callback",
    "decline_enrollment",
]


class ControlSpec(BaseModel):
    """One control. Without fail_next or rate it applies every time until reset."""

    type: ControlType
    scope: FaultScope = Field(default_factory=FaultScope)
    fail_next: int | None = Field(None, ge=1, description="Apply to the next N requests only")
    rate: float | None = Field(None, gt=0, le=1, description="Probability per request")
    seed: int | None = Field(None, description="RNG seed for rate mode")
    delay_ms: int = Field(DEFAULT_DELAY_MS, ge=0, le=120_000, description="callback_delay")
    status: int = Field(503, ge=500, le=599, description="error: the status to return")
    timeout_ms: int = Field(10_000, ge=0, le=120_000, description="timeout: how long to hang")
    reason: str = Field("DECLINED_BY_OPERATOR", max_length=100, description="decline reason")


class Record(BaseModel):
    operator_ref: str
    enrollment_id: uuid.UUID
    card_id: uuid.UUID
    token_ref: str | None = None
    status: Literal["PROCESSING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"]
    last4: str
    expiry: str
    operator_version: int = 0
    contact: dict[str, Any] | None = None
    terms_version: str | None = None
    updated_at: str | None = None


class State:
    def __init__(self) -> None:
        self.records: dict[str, Record] = {}  # operator_ref -> record
        self.by_enrollment: dict[uuid.UUID, str] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}  # key -> fingerprint, response
        self.last_callback: dict[str, dict[str, Any]] = {}  # operator_ref -> last body
        self.previous_callback: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []  # recent callbacks, newest last
        self.tasks: set[asyncio.Task] = set()
        self.controls = FaultRegistry()

    def reset(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()
        self.records.clear()
        self.by_enrollment.clear()
        self.idempotency.clear()
        self.last_callback.clear()
        self.previous_callback.clear()
        self.sent.clear()
        self.controls.clear()

    def put(self, record: Record) -> None:
        record.updated_at = clock.now().isoformat()
        self.records[record.operator_ref] = record
        self.by_enrollment[record.enrollment_id] = record.operator_ref


state = State()


# ---- Inbound: signed, idempotent requests from the bank ---------------------------------


async def _authenticate(request: Request) -> bytes:
    raw = await request.body()
    hmac_auth.verify(
        SECRET,
        request.headers.get(hmac_auth.TIMESTAMP_HEADER),
        request.headers.get(hmac_auth.SIGNATURE_HEADER),
        raw,
    )
    return raw


async def _accept(request: Request, apply) -> JSONResponse:
    """Common path: controls, signature, idempotency, then ``apply`` (returns the record
    and a coroutine factory for the callback)."""
    method, path = request.method, request.url.path
    if spec := state.controls.take("timeout", method=method, path=path):
        await asyncio.sleep(spec.timeout_ms / 1000)
        raise AppError(504, "OPERATOR_TIMEOUT", "The operator did not answer in time")
    if spec := state.controls.take("error", method=method, path=path):
        raise AppError(spec.status, "OPERATOR_UNAVAILABLE", "Injected operator error")
    raw = await _authenticate(request)
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise AppError(422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
    fingerprint = hashlib.sha256(f"{method} {path}\n".encode() + raw).hexdigest()
    seen = state.idempotency.get(key)
    if seen is not None:
        if seen["fingerprint"] != fingerprint:
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Key already used for another request")
        if state.controls.take("duplicate_callback", method=method, path=path):
            ref = seen["response"]["operator_ref"]
            if ref in state.last_callback:
                _schedule(dict(state.last_callback[ref]), method, path, DEFAULT_DELAY_MS)
        return JSONResponse(status_code=202, content=seen["response"])
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise AppError(422, "VALIDATION_ERROR", "Body is not JSON") from None
    record, callback = await apply(body)
    response = {"operator_ref": record.operator_ref, "status": "ACCEPTED"}
    state.idempotency[key] = {"fingerprint": fingerprint, "response": response}
    delay = DEFAULT_DELAY_MS
    if spec := state.controls.take("callback_delay", method=method, path=path):
        delay = spec.delay_ms
    _spawn(_after(delay, callback, method, path))
    return JSONResponse(status_code=202, content=response)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


def _record(ref: str) -> Record:
    record = state.records.get(ref)
    if record is None:
        raise AppError(404, "ENROLLMENT_UNKNOWN", "No enrolment with this operator_ref")
    return record


def _callback_body(record: Record, reason: str | None = None) -> dict[str, Any]:
    status = "FAILED" if record.status == "FAILED" else record.status
    body: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "operator_ref": record.operator_ref,
        "enrollment_id": str(record.enrollment_id),
        "status": status,
        "operator_version": record.operator_version,
        "occurred_at": clock.now().isoformat(),
    }
    if record.token_ref and status == "ENROLLED":
        body["token_ref"] = record.token_ref
    if reason:
        body["reason"] = reason
    return body


async def _after(delay_ms: int, callback, method: str, path: str) -> None:
    await asyncio.sleep(delay_ms / 1000)
    body = await callback()
    if body is not None:
        await _send_with_controls(body, method, path)


def _schedule(body: dict[str, Any], method: str, path: str, delay_ms: int) -> None:
    async def run() -> None:
        await asyncio.sleep(delay_ms / 1000)
        await _send(body)

    _spawn(run())


async def _send_with_controls(body: dict[str, Any], method: str, path: str) -> None:
    ref = body["operator_ref"]
    if ref in state.last_callback:
        state.previous_callback[ref] = state.last_callback[ref]
    state.last_callback[ref] = body
    if state.controls.take("drop_callback", method=method, path=path):
        log.info("callback dropped by control", extra={"operator_ref": ref})
        _remember(body, "DROPPED")
        return
    bad = state.controls.take("bad_signature", method=method, path=path) is not None
    await _send(body, bad_signature=bad)
    if state.controls.take("duplicate_callback", method=method, path=path):
        await _send(body)
    if state.controls.take("out_of_order_callback", method=method, path=path):
        previous = state.previous_callback.get(ref)
        if previous is not None:
            await _send({**previous, "event_id": str(uuid.uuid4())})


async def _send(body: dict[str, Any], bad_signature: bool = False) -> None:
    raw = json.dumps(body, separators=(",", ":")).encode()
    secret = "not-the-shared-secret" if bad_signature else SECRET
    for attempt in range(1, CALLBACK_ATTEMPTS + 1):
        headers = {"Content-Type": "application/json", **hmac_auth.signature_headers(secret, raw)}
        try:
            async with http_client.client("", timeout=5.0) as c:
                resp = await c.post(BANK_CALLBACK_URL, content=raw, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("callback failed", extra={"attempt": attempt, "error": str(exc)})
        else:
            _remember(body, resp.status_code)
            if resp.status_code < 500:
                return
        await asyncio.sleep(attempt)


def _remember(body: dict[str, Any], outcome: Any) -> None:
    state.sent.append({**body, "delivery": outcome})
    del state.sent[:-200]


# ---- Operator API -----------------------------------------------------------------------

api = APIRouter(prefix="/v1/enrollments", tags=["enrollments"])
REF = Path(description="The operator's reference for the enrolment")
ACCEPTED = {
    202: {"description": "Accepted; a signed callback follows"},
    401: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
}


class EnrolBody(BaseModel):
    enrollment_id: uuid.UUID
    card_id: uuid.UUID
    last4: str = Field(pattern=r"^\d{4}$")
    expiry: str = Field(pattern=r"^\d{4}-\d{2}$")
    terms_version: str | None = None
    contact: dict[str, Any] | None = None


def _parse(model, body: dict):
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise AppError(422, "VALIDATION_ERROR", "Body is not valid", exc.errors()) from None


@api.post("", summary="Enrol a card", status_code=202, responses=ACCEPTED)
async def enrol(request: Request) -> JSONResponse:
    async def apply(raw: dict):
        b = _parse(EnrolBody, raw)
        ref = state.by_enrollment.get(b.enrollment_id) or seed_data.operator_ref(b.enrollment_id)
        old = state.records.get(ref)
        record = Record(
            operator_ref=ref,
            enrollment_id=b.enrollment_id,
            card_id=b.card_id,
            status="PROCESSING",
            last4=b.last4,
            expiry=b.expiry,
            operator_version=old.operator_version if old else 0,
            contact=b.contact,
            terms_version=b.terms_version,
        )
        state.put(record)
        decline = state.controls.take("decline_enrollment", method="POST", path=request.url.path)

        async def callback():
            record.operator_version += 1
            if decline is not None:
                record.status = "FAILED"
                state.put(record)
                return _callback_body(record, decline.reason)
            try:
                async with http_client.client(NETWORK_URL, service_key=SERVICE_KEY) as c:
                    resp = await c.post(
                        "/v1/tokens",
                        json={"card_id": str(b.card_id), "enrollment_id": str(b.enrollment_id)},
                    )
                resp.raise_for_status()
                record.token_ref = resp.json()["token_ref"]
                record.status = "ENROLLED"
                reason = None
            except (httpx.HTTPError, KeyError, ValueError):
                record.status = "FAILED"
                reason = "TOKEN_SERVICE_UNAVAILABLE"
            state.put(record)
            return _callback_body(record, reason)

        return record, callback

    return await _accept(request, apply)


def _change(new_status: str | None, fields: tuple[str, ...] = ()):
    """An operation on an existing record: optional field updates and a status change."""

    def make(ref: str):
        async def apply(raw: dict):
            record = _record(ref)
            if record.status == "REMOVED" and new_status != "REMOVED":
                raise AppError(422, "ENROLLMENT_REMOVED", "The enrolment was removed")

            async def callback():
                for name in fields:
                    if name in raw and raw[name] is not None:
                        setattr(record, name, raw[name])
                if new_status is not None:
                    record.status = new_status
                record.operator_version += 1
                state.put(record)
                return _callback_body(record)

            return record, callback

        return apply

    return make


@api.patch(
    "/{operator_ref}",
    summary="Update card details or contact",
    status_code=202,
    responses=ACCEPTED,
)
async def update(request: Request, operator_ref: str = REF) -> JSONResponse:
    return await _accept(request, _change(None, ("last4", "expiry", "contact"))(operator_ref))


@api.post("/{operator_ref}/suspend", summary="Suspend", status_code=202, responses=ACCEPTED)
async def suspend(request: Request, operator_ref: str = REF) -> JSONResponse:
    return await _accept(request, _change("SUSPENDED")(operator_ref))


@api.post("/{operator_ref}/resume", summary="Resume", status_code=202, responses=ACCEPTED)
async def resume(request: Request, operator_ref: str = REF) -> JSONResponse:
    return await _accept(request, _change("ENROLLED")(operator_ref))


@api.delete("/{operator_ref}", summary="Remove", status_code=202, responses=ACCEPTED)
async def remove(request: Request, operator_ref: str = REF) -> JSONResponse:
    return await _accept(request, _change("REMOVED")(operator_ref))


files = APIRouter(prefix="/v1/files", tags=["files"])
FILE_COLUMNS = (
    "operator_ref",
    "enrollment_id",
    "card_id",
    "token_ref",
    "status",
    "last4",
    "expiry",
    "operator_version",
    "updated_at",
)


@files.get(
    "/enrollments/{file_date}",
    summary="Daily enrolment file (CSV)",
    description="Every enrolment record the operator holds, one row each, as of the request. "
    "Used by the bank's reconciliation. HMAC-signed like every bank request (empty body).",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}, 401: {"model": ErrorBody}},
)
async def enrollment_file(request: Request, file_date: date) -> Response:
    await _authenticate(request)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(FILE_COLUMNS)
    for record in sorted(state.records.values(), key=lambda r: str(r.enrollment_id)):
        data = record.model_dump(mode="json")
        writer.writerow([data.get(c) or "" for c in FILE_COLUMNS])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="enrollments-{file_date.isoformat()}.csv"'
        },
    )


# ---- Test controls ------------------------------------------------------------------------

control_router = APIRouter(
    prefix="/test", tags=["test-support"], dependencies=[Depends(require_test_key)]
)


class BehaviourRequest(BaseModel):
    """Replaces every control. ``{"controls": []}`` turns them all off."""

    controls: list[ControlSpec] = Field(default_factory=list)


@control_router.get("/behaviour", summary="Read the active controls")
async def get_behaviour() -> dict:
    return {"controls": state.controls.list(), "default_delay_ms": DEFAULT_DELAY_MS}


@control_router.post("/behaviour", summary="Replace the active controls")
async def set_behaviour(body: BehaviourRequest) -> dict:
    state.controls.replace(body.controls)  # type: ignore[arg-type]
    return {"controls": state.controls.list(), "default_delay_ms": DEFAULT_DELAY_MS}


class RecordPatch(BaseModel):
    enrollment_id: uuid.UUID
    operator_ref: str | None = None
    card_id: uuid.UUID | None = None
    token_ref: str | None = None
    status: Literal["PROCESSING", "ENROLLED", "SUSPENDED", "REMOVED", "FAILED"] | None = None
    last4: str | None = Field(None, pattern=r"^\d{4}$")
    expiry: str | None = Field(None, pattern=r"^\d{4}-\d{2}$")
    operator_version: int | None = Field(None, ge=0)


class StateChange(BaseModel):
    """Make the operator's records differ from the bank's (for reconciliation), or add one."""

    upsert: list[RecordPatch] = Field(default_factory=list)
    delete: list[uuid.UUID] = Field(default_factory=list, description="enrollment_ids")


@control_router.get("/state", summary="Records, recent callbacks and pending work")
async def get_state() -> dict:
    return {
        "records": [r.model_dump(mode="json") for r in state.records.values()],
        "callbacks": state.sent[-50:],
        "pending_callbacks": len(state.tasks),
        "idempotency_keys": len(state.idempotency),
    }


@control_router.post(
    "/state",
    summary="Change records directly",
    description="Upserts or deletes records without any callback. A new record needs "
    "card_id, status, last4 and expiry.",
)
async def change_state(body: StateChange) -> dict:
    for patch in body.upsert:
        ref = state.by_enrollment.get(patch.enrollment_id)
        data = patch.model_dump(exclude_none=True)
        if ref is None:
            data.setdefault("operator_ref", seed_data.operator_ref(patch.enrollment_id))
            try:
                state.put(Record(**data))
            except ValidationError as exc:
                raise AppError(422, "VALIDATION_ERROR", "Incomplete record", exc.errors()) from None
        else:
            record = state.records[ref]
            for name, value in data.items():
                if name not in ("enrollment_id", "operator_ref"):
                    setattr(record, name, value)
            state.put(record)
    for enrollment_id in body.delete:
        ref = state.by_enrollment.pop(enrollment_id, None)
        if ref:
            state.records.pop(ref, None)
    return await get_state()


async def on_reset(_: Runtime) -> None:
    state.reset()


async def on_seed(_: Runtime) -> None:
    for card in seed_data.CARDS:
        e = card.enrollment
        if e is None:
            continue
        state.put(
            Record(
                operator_ref=seed_data.operator_ref(e.enrollment_id),
                enrollment_id=e.enrollment_id,
                card_id=card.card_id,
                token_ref=e.token_ref,
                status=e.status,  # type: ignore[arg-type]
                last4=card.last4,
                expiry=card.expiry,
                operator_version=e.version,
                terms_version=seed_data.TERMS_VERSION,
            )
        )


async def on_shutdown(_: Runtime) -> None:
    for task in list(state.tasks):
        task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*state.tasks, return_exceptions=True)


app = create_app(
    ServiceSpec(
        name="wallet-operator-mock",
        title="CardVault wallet-operator-mock",
        description=(
            "A controllable stand-in for the wallet operator: accepts enrolment requests from "
            "the bank (202), gets tokens from the network, sends signed status callbacks, and "
            "serves the daily enrolment file."
        ),
        routers=[api, files, control_router],
        uses_db=False,
        uses_kafka=False,
        on_reset=[on_reset],
        on_seed=[on_seed],
        on_shutdown=[on_shutdown],
    )
)
