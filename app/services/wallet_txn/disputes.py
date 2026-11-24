"""Opening a dispute: ``POST /v1/wallet/transactions/{txn_id}/disputes`` (multipart).

A deliberately small record (spec 6.4): a reason, an optional note and an optional receipt.
The receipt's type comes from its first bytes, never from its name or the client's
Content-Type, so a PNG named ``receipt.pdf`` is stored as a PNG (spec 16.10 rule 63).
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from services.wallet_txn.api import (
    ERRORS,
    TXN_ID,
    DisputeReason,
    DisputeView,
    dispute_view,
    own_transaction,
)
from services.wallet_txn.domain import DISPUTABLE
from services.wallet_txn.models import disputes, idempotency_keys
from shared import bugs, clock, idempotency
from shared.errors import AppError
from shared.security import Customer, require_customer

router = APIRouter(prefix="/v1/wallet", tags=["disputes"])

MAX_RECEIPT_BYTES = 5 * 1024 * 1024  # 5,242,880, inclusive
MAX_NOTE = 500
SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


BY_EXTENSION = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def sniff(data: bytes) -> str | None:
    for magic, content_type in SIGNATURES:
        if data.startswith(magic):
            return content_type
    return None


async def _read_receipt(receipt: UploadFile | None) -> tuple[bytes, str, str] | None:
    """(bytes, filename, detected type), or None when no file was chosen."""
    if receipt is None:
        return None
    data = await receipt.read(MAX_RECEIPT_BYTES + 1)
    if not data and not receipt.filename:
        return None  # a browser form with no file selected sends an empty part
    if len(data) > MAX_RECEIPT_BYTES:
        raise AppError(
            422,
            "RECEIPT_TOO_LARGE",
            "The receipt is larger than 5 MB",
            {"max_bytes": MAX_RECEIPT_BYTES},
        )
    content_type = sniff(data)
    if bugs.enabled("BUG_DISPUTE_FILE_TYPE_BY_NAME"):
        # Seeded bug: trusts the file name, so a renamed file of any content is accepted.
        content_type = BY_EXTENSION.get((receipt.filename or "").rsplit(".", 1)[-1].lower())
    if content_type is None:
        raise AppError(
            422,
            "RECEIPT_TYPE_NOT_ALLOWED",
            "The receipt must be a PDF, PNG or JPEG file",
            {"allowed": [t for _, t in SIGNATURES]},
        )
    filename = (receipt.filename or "receipt")[:255]
    return data, filename, content_type


@router.post(
    "/transactions/{txn_id}/disputes",
    summary="Dispute one of my transactions",
    description=(
        "multipart/form-data: `reason` (UNRECOGNISED, DUPLICATE, WRONG_AMOUNT or NOT_RECEIVED), "
        "optional `note` (at most 500 characters) and optional `receipt` (PDF, PNG or JPEG, at "
        "most 5,242,880 bytes, type detected from the content). Only AUTHORIZED, SETTLED, "
        "PARTIALLY_REVERSED and PARTIALLY_REFUNDED transactions can be disputed (otherwise 422 "
        "TRANSACTION_NOT_DISPUTABLE), and only once while a dispute is OPEN (409 "
        "DISPUTE_ALREADY_OPEN). Accepts `Idempotency-Key`; the fingerprint is the fields and the "
        "receipt's SHA-256, so a retry of the same form replays the first answer."
    ),
    status_code=201,
    response_model=DisputeView,
    responses={409: ERRORS[404], **ERRORS},
)
async def open_dispute(
    request: Request,
    txn_id: uuid.UUID = TXN_ID,
    reason: DisputeReason = Form(description="Why the transaction is disputed"),
    note: str | None = Form(None, max_length=MAX_NOTE, description="At most 500 characters"),
    receipt: UploadFile | None = File(None, description="PDF, PNG or JPEG, up to 5 MB"),
    customer: Customer = Depends(require_customer),
) -> Response:
    db = request.app.state.runtime.db
    upload = await _read_receipt(receipt)
    note = note or None
    fingerprint = b"\n".join(
        [
            reason.encode(),
            (note or "").encode(),
            hashlib.sha256(upload[0]).hexdigest().encode() if upload else b"-",
        ]
    )

    async def handler():
        now = clock.now()
        dispute_id = uuid.uuid4()
        try:
            async with db.transaction() as s:
                txn = await own_transaction(s, txn_id, customer, lock=True)
                if txn.status not in DISPUTABLE:
                    raise AppError(
                        422,
                        "TRANSACTION_NOT_DISPUTABLE",
                        f"A {txn.status} transaction cannot be disputed",
                        {"status": txn.status, "disputable": list(DISPUTABLE)},
                    )
                if txn.open_dispute_id is not None:
                    raise _already_open(txn.open_dispute_id)
                row = (
                    await s.execute(
                        disputes.insert()
                        .values(
                            dispute_id=dispute_id,
                            txn_id=txn_id,
                            customer_id=customer.customer_id,
                            reason=reason,
                            note=note,
                            receipt=upload[0] if upload else None,
                            receipt_filename=upload[1] if upload else None,
                            receipt_content_type=upload[2] if upload else None,
                            receipt_size=len(upload[0]) if upload else None,
                            status="OPEN",
                            created_at=now,
                        )
                        .returning(*[c for c in disputes.c if c.name != "receipt"])
                    )
                ).one()
        except IntegrityError:
            # Lost a race with another request for the same transaction (unique index).
            raise _already_open(None) from None
        return 201, dispute_view(row)

    return await idempotency.run(
        db,
        idempotency_keys,
        request,
        str(customer.customer_id),
        handler,
        fingerprint_body=fingerprint,
    )


def _already_open(dispute_id: uuid.UUID | None) -> AppError:
    return AppError(
        409,
        "DISPUTE_ALREADY_OPEN",
        "This transaction already has an open dispute",
        {"dispute_id": str(dispute_id)} if dispute_id else None,
    )
