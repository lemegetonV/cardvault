"""card-service tables (SQLAlchemy Core). Amounts are integer cents."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from shared.idempotency import idempotency_table
from shared.outbox import outbox_table

metadata = sa.MetaData()

cards = sa.Table(
    "cards",
    metadata,
    sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("product", sa.String(16), nullable=False),  # CREDIT | DEBIT | PREPAID
    sa.Column("pan_encrypted", sa.Text, nullable=False),  # AES-GCM, never leaves this service
    sa.Column("last4", sa.String(4), nullable=False),
    sa.Column("expiry", sa.String(7), nullable=False),  # YYYY-MM
    sa.Column("status", sa.String(16), nullable=False),  # ACTIVE | LOST | CLOSED
    sa.Column("limit_amount", sa.BigInteger, nullable=False),
    sa.Column("available_amount", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    sa.Column("last_used_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

holds = sa.Table(
    "holds",
    metadata,
    sa.Column("hold_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("card_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
    sa.Column("original_amount", sa.BigInteger, nullable=False),
    sa.Column("amount", sa.BigInteger, nullable=False),  # what is still held
    sa.Column("status", sa.String(16), nullable=False),  # ACTIVE | SETTLED | RELEASED
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# One row per trace_id: the decision is stored, so a duplicate returns it unchanged.
authorizations = sa.Table(
    "authorizations",
    metadata,
    sa.Column("trace_id", sa.String(64), primary_key=True),
    sa.Column("card_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
    sa.Column("token_ref", sa.String(64)),
    sa.Column("token_requestor", sa.String(16), nullable=False),
    sa.Column("amount", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("merchant_name", sa.Text, nullable=False),
    sa.Column("mcc", sa.String(4), nullable=False),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("decline_code", sa.String(32)),
    sa.Column("hold_id", UUID(as_uuid=True)),
    sa.Column("cleared_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reversed_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("refunded_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("settled_at", sa.DateTime(timezone=True)),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("response", JSONB, nullable=False),
)

processed_clearing = sa.Table(
    "processed_clearing",
    metadata,
    sa.Column("clearing_id", sa.String(64), primary_key=True),
    sa.Column("trace_id", sa.String(64), nullable=False, index=True),
    sa.Column("type", sa.String(16), nullable=False),
    sa.Column("amount", sa.BigInteger, nullable=False),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("response", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Local, eventually consistent copy of wallet token status, fed by enrollment.changed.
wallet_tokens = sa.Table(
    "wallet_tokens",
    metadata,
    sa.Column("token_ref", sa.String(64), primary_key=True),
    sa.Column("card_id", UUID(as_uuid=True), nullable=False),
    sa.Column("enrollment_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

outbox = outbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

RESET_TABLES = [
    "cards",
    "holds",
    "authorizations",
    "processed_clearing",
    "wallet_tokens",
    "outbox",
    "idempotency_keys",
]
