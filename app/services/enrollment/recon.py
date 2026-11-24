"""Reconciliation: the enrolment SOR against the wallet operator's daily enrolment file.

``POST /internal/v1/reconciliation/runs {file_date}`` fetches the file (HMAC-signed GET),
compares it with the ``enrollments`` table and stores the run and its findings:

* ``MISSING_AT_OPERATOR``: ENROLLED or SUSPENDED in the SOR, absent from the file.
* ``MISSING_IN_SOR``: in the file, unknown to the SOR.
* ``STATUS_MISMATCH``: both know it, with different statuses.
* ``DATA_MISMATCH``: same status, but ``last4`` or ``expiry`` differ (ENROLLED/SUSPENDED).

Enrolments that are in flight (PENDING, or with a push still waiting for its callback) are
skipped and counted, because the two sides are expected to differ until the callback lands.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import date, datetime

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from services.enrollment.models import (
    enrollments,
    reconciliation_findings,
    reconciliation_runs,
)
from services.enrollment.operator_client import OPERATOR_URL, SECRET, TIMEOUT
from shared import bugs, clock, hmac_auth, http_client
from shared.errors import AppError, ErrorBody
from shared.security import require_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1/reconciliation", tags=["reconciliation"])
service_auth = require_service("tests")

ACTIVE = ("ENROLLED", "SUSPENDED")


class RunRequest(BaseModel):
    file_date: date | None = Field(
        None, description="Business date of the operator file; default: today on the shared clock"
    )


class Finding(BaseModel):
    type: str = Field(
        description="MISSING_AT_OPERATOR, MISSING_IN_SOR, STATUS_MISMATCH or DATA_MISMATCH"
    )
    enrollment_id: uuid.UUID | None
    card_id: uuid.UUID | None
    operator_ref: str | None
    sor_status: str | None
    operator_status: str | None
    details: dict = Field(description="For DATA_MISMATCH: each differing field, both values")


class Run(BaseModel):
    run_id: uuid.UUID
    file_date: date
    status: str = Field(description="COMPLETED or FAILED (the file could not be fetched)")
    sor_records: int
    file_records: int
    skipped_in_flight: int = Field(description="PENDING or awaiting a callback; not compared")
    finding_count: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    findings: list[Finding]


async def fetch_file(file_date: date) -> list[dict[str, str]]:
    path = f"/v1/files/enrollments/{file_date.isoformat()}"
    async with http_client.client(OPERATOR_URL, timeout=TIMEOUT) as c:
        resp = await c.get(path, headers=hmac_auth.signature_headers(SECRET, b""))
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def compare(sor: list, file_rows: list[dict[str, str]]) -> tuple[list[Finding], int]:
    """Pure comparison. Returns the findings and how many SOR rows were skipped."""
    findings: list[Finding] = []
    by_id = {r["enrollment_id"]: r for r in file_rows if r.get("enrollment_id")}
    known = set()
    skipped = 0
    for e in sor:
        eid = str(e.enrollment_id)
        known.add(eid)
        op = by_id.get(eid)
        if e.status == "PENDING" or e.pending_action is not None:
            skipped += 1
            continue
        base = {
            "enrollment_id": e.enrollment_id,
            "card_id": e.card_id,
            "operator_ref": e.operator_ref or (op or {}).get("operator_ref") or None,
            "sor_status": e.status,
            "operator_status": (op or {}).get("status") or None,
        }
        if op is None:
            if e.status in ACTIVE:
                findings.append(Finding(type="MISSING_AT_OPERATOR", details={}, **base))
            continue
        if op.get("status") != e.status:
            # Seeded bug: status differences are not reported (nor compared further).
            if not bugs.enabled("BUG_RECON_MISSES_STATUS_MISMATCH"):
                findings.append(Finding(type="STATUS_MISMATCH", details={}, **base))
            continue
        if e.status in ACTIVE:
            diff = {
                field: {"sor": getattr(e, field), "operator": op.get(field) or None}
                for field in ("last4", "expiry")
                if (op.get(field) or None) != getattr(e, field)
            }
            if diff:
                findings.append(Finding(type="DATA_MISMATCH", details=diff, **base))
    for eid, op in by_id.items():
        if eid not in known:
            findings.append(
                Finding(
                    type="MISSING_IN_SOR",
                    enrollment_id=_uuid(eid),
                    card_id=_uuid(op.get("card_id")),
                    operator_ref=op.get("operator_ref") or None,
                    sor_status=None,
                    operator_status=op.get("status") or None,
                    details={},
                )
            )
    return findings, skipped


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@router.post(
    "/runs",
    summary="Run reconciliation now",
    description="Fetches the operator's enrolment file for `file_date` and compares it with the "
    "SOR. 201 with the run and its findings. If the file cannot be fetched the run is stored "
    "as FAILED and 502 OPERATOR_FILE_UNAVAILABLE is returned with its run_id. Tests key only.",
    status_code=201,
    response_model=Run,
    responses={401: {"model": ErrorBody}, 502: {"model": ErrorBody}},
)
async def start_run(
    request: Request, body: RunRequest | None = None, _: str = Depends(service_auth)
) -> Run:
    db = request.app.state.runtime.db
    started = clock.now()
    file_date = (body.file_date if body else None) or started.date()
    run_id = uuid.uuid4()
    try:
        file_rows = await fetch_file(file_date)
    except (httpx.HTTPError, csv.Error) as exc:
        log.warning("operator file unavailable", extra={"error": str(exc)})
        async with db.transaction() as s:
            await s.execute(
                reconciliation_runs.insert().values(
                    run_id=run_id,
                    file_date=file_date,
                    status="FAILED",
                    error=str(exc)[:500] or type(exc).__name__,
                    started_at=started,
                    finished_at=clock.now(),
                )
            )
        raise AppError(
            502,
            "OPERATOR_FILE_UNAVAILABLE",
            "The operator's enrolment file could not be fetched",
            {"run_id": str(run_id)},
        ) from None

    async with db.transaction() as s:
        sor = (await s.execute(sa.select(enrollments))).all()
        findings, skipped = compare(sor, file_rows)
        await s.execute(
            reconciliation_runs.insert().values(
                run_id=run_id,
                file_date=file_date,
                status="COMPLETED",
                sor_records=len(sor),
                file_records=len(file_rows),
                skipped_in_flight=skipped,
                finding_count=len(findings),
                started_at=started,
                finished_at=clock.now(),
            )
        )
        if findings:
            await s.execute(
                reconciliation_findings.insert(),
                [{"run_id": run_id, **f.model_dump(mode="json")} for f in findings],
            )
    log.info("reconciliation finished", extra={"run_id": str(run_id), "findings": len(findings)})
    return await _load(db, run_id)


@router.get(
    "/runs/{run_id}",
    summary="Get a reconciliation run and its findings",
    response_model=Run,
    responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def get_run(
    request: Request,
    run_id: uuid.UUID = Path(description="run_id from POST /runs"),
    _: str = Depends(service_auth),
) -> Run:
    return await _load(request.app.state.runtime.db, run_id)


async def _load(db, run_id: uuid.UUID) -> Run:
    async with db.session() as s:
        run = (
            await s.execute(
                sa.select(reconciliation_runs).where(reconciliation_runs.c.run_id == run_id)
            )
        ).first()
        if run is None:
            raise AppError(404, "RUN_NOT_FOUND", "Reconciliation run not found")
        rows = (
            await s.execute(
                sa.select(reconciliation_findings)
                .where(reconciliation_findings.c.run_id == run_id)
                .order_by(reconciliation_findings.c.finding_id)
            )
        ).all()
    return Run(
        **{k: v for k, v in run._mapping.items()},
        findings=[
            Finding(
                type=r.type,
                enrollment_id=r.enrollment_id,
                card_id=r.card_id,
                operator_ref=r.operator_ref,
                sor_status=r.sor_status,
                operator_status=r.operator_status,
                details=r.details,
            )
            for r in rows
        ],
    )
