"""wallet-txn-service initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from shared.idempotency import create_idempotency_table

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
STATUSES = (
    "'AUTHORIZED','DECLINED','SETTLED','PARTIALLY_REVERSED','REVERSED',"
    "'REFUNDED','PARTIALLY_REFUNDED'"
)


def upgrade() -> None:
    op.create_table(
        "wallet_transactions",
        sa.Column("txn_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_ref", sa.String(64)),
        sa.Column("merchant_name", sa.Text),
        sa.Column("mcc", sa.String(4)),
        sa.Column("decision", sa.String(16)),
        sa.Column("decline_code", sa.String(32)),
        sa.Column("auth_amount", sa.BigInteger),
        sa.Column("cleared_amount", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reversed_amount", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("refunded_amount", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("settled_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_wallet_txn_status"),
    )
    op.create_index(
        "ix_wallet_txn_customer_time",
        "wallet_transactions",
        ["customer_id", sa.text("occurred_at DESC"), sa.text("txn_id DESC")],
    )
    op.create_table(
        "processed_events",
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("processed_at", TS, nullable=False),
    )
    op.create_table(
        "disputes",
        sa.Column("dispute_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("txn_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("note", sa.String(500)),
        sa.Column("receipt", sa.LargeBinary),
        sa.Column("receipt_filename", sa.String(255)),
        sa.Column("receipt_content_type", sa.String(32)),
        sa.Column("receipt_size", sa.Integer),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(
            "reason IN ('UNRECOGNISED','DUPLICATE','WRONG_AMOUNT','NOT_RECEIVED')",
            name="ck_disputes_reason",
        ),
        sa.CheckConstraint("receipt_size IS NULL OR receipt_size <= 5242880", name="ck_size"),
    )
    op.create_index("ix_disputes_customer", "disputes", ["customer_id", "created_at"])
    # One open dispute per transaction (spec 6.4): enforced by the database.
    op.create_index(
        "ux_disputes_open_txn",
        "disputes",
        ["txn_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    create_idempotency_table(op)


def downgrade() -> None:
    for table in ("idempotency_keys", "disputes", "processed_events", "wallet_transactions"):
        op.drop_table(table)
