"""wallet-txn-service customer API: wallet transactions, the CSV export and disputes.

Every query is scoped to the customer in the JWT. Another customer's transaction or
dispute returns 404, exactly like one that does not exist (so IDs cannot be probed).
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.wallet_txn.models import disputes, wallet_transactions
from shared import pagination
from shared.errors import AppError, ErrorBody
from shared.security import Customer, require_customer

router = APIRouter(prefix="/v1/wallet", tags=["transactions"])

TxnStatus = Literal[
    "AUTHORIZED",
    "DECLINED",
    "SETTLED",
    "PARTIALLY_REVERSED",
    "REVERSED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
]
DisputeReason = Literal["UNRECOGNISED", "DUPLICATE", "WRONG_AMOUNT", "NOT_RECEIVED"]

ERRORS = {
    400: {"model": ErrorBody, "description": "Unreadable cursor"},
    401: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}
TXN_ID = Path(description="The transaction's txn_id (UUID)")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TransactionView(BaseModel):
    txn_id: uuid.UUID
    trace_id: str = Field(description="The network's trace ID for the purchase")
    card_id: uuid.UUID
    merchant_name: str | None = Field(
        description="Untrusted free text exactly as the merchant sent it. null only while the "
        "authorization event has not arrived yet."
    )
    mcc: str | None
    status: TxnStatus = Field(description="Derived from the amounts; see ARCHITECTURE.md")
    decline_code: str | None
    amount: int | None = Field(description="Authorized amount in cents")
    cleared_amount: int = Field(description="Settled amount in cents (0 until settled)")
    reversed_amount: int
    refunded_amount: int
    currency: str
    occurred_at: datetime = Field(description="When the purchase was authorized (UTC)")
    settled_at: datetime | None
    open_dispute_id: uuid.UUID | None = Field(description="The OPEN dispute, if there is one")


class TransactionPage(BaseModel):
    items: list[TransactionView]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null at end")


class Filters(BaseModel):
    card_ids: list[uuid.UUID]
    statuses: list[str]
    start: datetime | None
    end: datetime | None  # exclusive
    from_label: str
    to_label: str


def _bound(raw: str | None, name: str, *, is_end: bool) -> tuple[datetime | None, str]:
    """``from``/``to``: a UTC datetime with an offset, or a date (a whole UTC day).

    Both bounds are inclusive (spec 16.8 rule 55). Returns the bound (``to`` is turned into
    an exclusive one) and the date used in the export filename.
    """
    if raw is None or raw == "":
        return None, "all"
    if _DATE_ONLY.match(raw):
        try:
            day = datetime.fromisoformat(raw).date()
        except ValueError:
            raise _bad(name) from None
        start = datetime.combine(day, time(0), tzinfo=UTC)
        return (start + timedelta(days=1) if is_end else start), day.isoformat()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _bad(name) from None
    if value.tzinfo is None:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            f"`{name}` needs a timezone offset (for example 2026-11-01T00:00:00Z)",
            {"field": name},
        )
    value = value.astimezone(UTC)
    # An inclusive end: anything up to and including this instant.
    return (value + timedelta(microseconds=1) if is_end else value), value.date().isoformat()


def _bad(name: str) -> AppError:
    return AppError(
        422, "VALIDATION_ERROR", f"`{name}` must be a date or an ISO 8601 datetime", {"field": name}
    )


def filters(
    card_id: list[uuid.UUID] | None = Query(None, description="Repeat for several cards"),
    status: list[TxnStatus] | None = Query(None, description="Repeat for several statuses"),
    from_: str | None = Query(
        None,
        alias="from",
        description="Inclusive lower bound: an ISO 8601 datetime with offset, or a date "
        "(start of that UTC day)",
    ),
    to: str | None = Query(
        None,
        description="Inclusive upper bound: an ISO 8601 datetime with offset, or a date "
        "(end of that UTC day)",
    ),
) -> Filters:
    start, from_label = _bound(from_, "from", is_end=False)
    end, to_label = _bound(to, "to", is_end=True)
    if start and end and start >= end:
        raise AppError(422, "VALIDATION_ERROR", "`from` must not be after `to`", {"field": "from"})
    return Filters(
        card_ids=card_id or [],
        statuses=list(status or []),
        start=start,
        end=end,
        from_label=from_label,
        to_label=to_label,
    )


def _query(customer: Customer, f: Filters):
    t = wallet_transactions
    open_dispute = (
        sa.select(disputes.c.dispute_id)
        .where(disputes.c.txn_id == t.c.txn_id, disputes.c.status == "OPEN")
        .limit(1)
        .scalar_subquery()
    )
    q = sa.select(t, open_dispute.label("open_dispute_id")).where(
        t.c.customer_id == customer.customer_id
    )
    if f.card_ids:
        q = q.where(t.c.card_id.in_(f.card_ids))
    if f.statuses:
        q = q.where(t.c.status.in_(f.statuses))
    if f.start:
        q = q.where(t.c.occurred_at >= f.start)
    if f.end:
        q = q.where(t.c.occurred_at < f.end)
    return q.order_by(t.c.occurred_at.desc(), t.c.txn_id.desc())


def view(row) -> TransactionView:
    return TransactionView(
        txn_id=row.txn_id,
        trace_id=row.trace_id,
        card_id=row.card_id,
        merchant_name=row.merchant_name,
        mcc=row.mcc,
        status=row.status,
        decline_code=row.decline_code,
        amount=row.auth_amount,
        cleared_amount=row.cleared_amount,
        reversed_amount=row.reversed_amount,
        refunded_amount=row.refunded_amount,
        currency=row.currency,
        occurred_at=row.occurred_at,
        settled_at=row.settled_at,
        open_dispute_id=row.open_dispute_id,
    )


def _db(request: Request):
    return request.app.state.runtime.db


@router.get(
    "/transactions",
    summary="List my wallet transactions",
    description="Newest first (ties broken by txn_id), 20 per page by default. `card_id` and "
    "`status` may repeat. `from` and `to` are inclusive. Only the caller's transactions.",
    response_model=TransactionPage,
    responses=ERRORS,
)
async def list_transactions(
    request: Request,
    f: Filters = Depends(filters),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    limit: int = Depends(pagination.limit_param),
    customer: Customer = Depends(require_customer),
) -> TransactionPage:
    q = _query(customer, f)
    t = wallet_transactions
    if cursor:
        ts, last_id = pagination.decode(cursor)
        try:
            last = uuid.UUID(last_id)
        except ValueError:
            raise AppError(400, "INVALID_CURSOR", "The cursor is not valid") from None
        q = q.where(sa.tuple_(t.c.occurred_at, t.c.txn_id) < sa.tuple_(sa.literal(ts), last))
    async with _db(request).session() as s:
        rows = (await s.execute(q.limit(limit + 1))).all()
    page = rows[:limit]
    next_cursor = (
        pagination.encode(page[-1].occurred_at, page[-1].txn_id) if len(rows) > limit else None
    )
    return TransactionPage(items=[view(r) for r in page], next_cursor=next_cursor)


# ---- CSV export --------------------------------------------------------------------------

EXPORT_COLUMNS = (
    "txn_id",
    "occurred_at_utc",
    "card_id",
    "merchant_name",
    "mcc",
    "status",
    "decline_code",
    "amount",
    "cleared_amount",
    "reversed_amount",
    "refunded_amount",
    "currency",
    "settled_at_utc",
)
EXPORT_MAX_ROWS = 10_000
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def dollars(cents: int | None) -> str:
    """Integer cents to a dollar string with 2 decimals, without floats."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole}.{part:02d}"


def utc(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def safe_cell(value: str) -> str:
    """Spec 16.6 rule 42: a cell a spreadsheet would run as a formula gets a leading '."""
    return "'" + value if value.startswith(_FORMULA_START) else value


@router.get(
    "/transactions/export",
    summary="Export my wallet transactions as CSV",
    description=(
        "Same filters as the list (no paging; at most 10,000 rows, newest first). Columns: "
        + ", ".join(EXPORT_COLUMNS)
        + ". Amounts are dollars with 2 decimals, times are UTC (ISO 8601 with Z). A cell that "
        "starts with =, +, -, @, tab or CR is prefixed with ' so spreadsheets do not run it. "
        'Filename: wallet-transactions-<from>-<to>.csv, with "all" for an omitted bound.'
    ),
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}, **ERRORS},
)
async def export_transactions(
    request: Request,
    f: Filters = Depends(filters),
    customer: Customer = Depends(require_customer),
) -> Response:
    async with _db(request).session() as s:
        rows = (await s.execute(_query(customer, f).limit(EXPORT_MAX_ROWS))).all()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                safe_cell(str(c))
                for c in (
                    r.txn_id,
                    utc(r.occurred_at),
                    r.card_id,
                    r.merchant_name or "",
                    r.mcc or "",
                    r.status,
                    r.decline_code or "",
                    dollars(r.auth_amount),
                    dollars(r.cleared_amount),
                    dollars(r.reversed_amount),
                    dollars(r.refunded_amount),
                    r.currency,
                    utc(r.settled_at),
                )
            ]
        )
    filename = f"wallet-transactions-{f.from_label}-{f.to_label}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/transactions/{txn_id}",
    summary="Get one of my wallet transactions",
    description="404 if it does not exist or belongs to another customer.",
    response_model=TransactionView,
    responses=ERRORS,
)
async def get_transaction(
    request: Request,
    txn_id: uuid.UUID = TXN_ID,
    customer: Customer = Depends(require_customer),
) -> TransactionView:
    async with _db(request).session() as s:
        return view(await own_transaction(s, txn_id, customer))


async def own_transaction(session, txn_id: uuid.UUID, customer: Customer, lock: bool = False):
    q = _query(customer, _NO_FILTERS).where(wallet_transactions.c.txn_id == txn_id)
    row = (await session.execute(q)).first()
    if row is None:
        raise AppError(404, "TRANSACTION_NOT_FOUND", "Transaction not found")
    if lock:
        await session.execute(
            sa.select(wallet_transactions.c.txn_id)
            .where(wallet_transactions.c.txn_id == txn_id)
            .with_for_update()
        )
    return row


_NO_FILTERS = Filters(
    card_ids=[], statuses=[], start=None, end=None, from_label="all", to_label="all"
)


# ---- Disputes (read side; the upload is in disputes.py) -------------------------------------


class ReceiptMeta(BaseModel):
    filename: str
    content_type: Literal["application/pdf", "image/png", "image/jpeg"] = Field(
        description="Detected from the file's content, not its name"
    )
    size: int = Field(description="Bytes")


class DisputeView(BaseModel):
    dispute_id: uuid.UUID
    txn_id: uuid.UUID
    reason: DisputeReason
    note: str | None
    status: Literal["OPEN"]
    receipt: ReceiptMeta | None = Field(description="Metadata only; the file is never returned")
    created_at: datetime


class DisputePage(BaseModel):
    items: list[DisputeView]
    next_cursor: str | None


DISPUTE_COLUMNS = [
    c
    for c in disputes.c
    if c.name != "receipt"  # never load the bytes for a read
]


def dispute_view(row) -> DisputeView:
    receipt = None
    if row.receipt_filename is not None:
        receipt = ReceiptMeta(
            filename=row.receipt_filename,
            content_type=row.receipt_content_type,
            size=row.receipt_size,
        )
    return DisputeView(
        dispute_id=row.dispute_id,
        txn_id=row.txn_id,
        reason=row.reason,
        note=row.note,
        status=row.status,
        receipt=receipt,
        created_at=row.created_at,
    )


@router.get(
    "/disputes",
    tags=["disputes"],
    summary="List my disputes",
    description="Newest first. Receipt metadata only.",
    response_model=DisputePage,
    responses=ERRORS,
)
async def list_disputes(
    request: Request,
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    limit: int = Depends(pagination.limit_param),
    customer: Customer = Depends(require_customer),
) -> DisputePage:
    d = disputes
    q = sa.select(*DISPUTE_COLUMNS).where(d.c.customer_id == customer.customer_id)
    if cursor:
        ts, last_id = pagination.decode(cursor)
        try:
            last = uuid.UUID(last_id)
        except ValueError:
            raise AppError(400, "INVALID_CURSOR", "The cursor is not valid") from None
        q = q.where(sa.tuple_(d.c.created_at, d.c.dispute_id) < sa.tuple_(sa.literal(ts), last))
    q = q.order_by(d.c.created_at.desc(), d.c.dispute_id.desc()).limit(limit + 1)
    async with _db(request).session() as s:
        rows = (await s.execute(q)).all()
    page = rows[:limit]
    next_cursor = (
        pagination.encode(page[-1].created_at, page[-1].dispute_id) if len(rows) > limit else None
    )
    return DisputePage(items=[dispute_view(r) for r in page], next_cursor=next_cursor)


@router.get(
    "/disputes/{dispute_id}",
    tags=["disputes"],
    summary="Get one of my disputes",
    description="404 if it does not exist or belongs to another customer.",
    response_model=DisputeView,
    responses=ERRORS,
)
async def get_dispute(
    request: Request,
    dispute_id: uuid.UUID = Path(description="The dispute's ID"),
    customer: Customer = Depends(require_customer),
) -> DisputeView:
    async with _db(request).session() as s:
        row = (
            await s.execute(
                sa.select(*DISPUTE_COLUMNS).where(
                    disputes.c.dispute_id == dispute_id,
                    disputes.c.customer_id == customer.customer_id,
                )
            )
        ).first()
    if row is None:
        raise AppError(404, "DISPUTE_NOT_FOUND", "Dispute not found")
    return dispute_view(row)
