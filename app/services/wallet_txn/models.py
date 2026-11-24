"""wallet-txn-service tables (SQLAlchemy Core): the wallet transaction system of record."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from shared.idempotency import idempotency_table

metadata = sa.MetaData()
TS = sa.DateTime(timezone=True)

# One row per trace_id. Built from auth.decided and txn.cleared in whatever order they
# arrive, so the columns that only auth.decided knows are nullable.
wallet_transactions = sa.Table(
    "wallet_transactions",
    metadata,
    sa.Column("txn_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
    sa.Column("card_id", UUID(as_uuid=True), nullable=False),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
    sa.Column("token_ref", sa.String(64)),
    sa.Column("merchant_name", sa.Text),  # untrusted free text, stored as-is
    sa.Column("mcc", sa.String(4)),
    sa.Column("decision", sa.String(16)),  # APPROVED | DECLINED; null until auth.decided
    sa.Column("decline_code", sa.String(32)),
    sa.Column("auth_amount", sa.BigInteger),
    sa.Column("cleared_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reversed_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("refunded_amount", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    # AUTHORIZED | DECLINED | SETTLED | PARTIALLY_REVERSED | REVERSED | REFUNDED |
    # PARTIALLY_REFUNDED, always derived from the amounts (spec 16.1 rule 6)
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("occurred_at", TS, nullable=False),  # decided_at, or first clearing if earlier
    sa.Column("settled_at", TS),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
)

processed_events = sa.Table(
    "processed_events",
    metadata,
    sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("topic", sa.String(64), nullable=False),
    sa.Column("processed_at", TS, nullable=False),
)

disputes = sa.Table(
    "disputes",
    metadata,
    sa.Column("dispute_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("txn_id", UUID(as_uuid=True), nullable=False),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
    sa.Column("reason", sa.String(16), nullable=False),
    sa.Column("note", sa.String(500)),
    sa.Column("receipt", sa.LargeBinary),
    sa.Column("receipt_filename", sa.String(255)),
    sa.Column("receipt_content_type", sa.String(32)),
    sa.Column("receipt_size", sa.Integer),
    sa.Column("status", sa.String(16), nullable=False),  # OPEN
    sa.Column("created_at", TS, nullable=False),
)

idempotency_keys = idempotency_table(metadata)

RESET_TABLES = ["wallet_transactions", "processed_events", "disputes", "idempotency_keys"]
