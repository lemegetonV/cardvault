"""card-service initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from shared.idempotency import create_idempotency_table
from shared.outbox import create_outbox_table

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cards",
        sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("pan_encrypted", sa.Text, nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("expiry", sa.String(7), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("limit_amount", sa.BigInteger, nullable=False),
        sa.Column("available_amount", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("last_used_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("product IN ('CREDIT','DEBIT','PREPAID')", name="ck_cards_product"),
        sa.CheckConstraint("status IN ('ACTIVE','LOST','CLOSED')", name="ck_cards_status"),
    )
    op.create_index("ix_cards_customer_id", "cards", ["customer_id"])
    op.create_index("ix_cards_customer_page", "cards", ["customer_id", "created_at", "card_id"])
    op.create_table(
        "holds",
        sa.Column("hold_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
        sa.Column("original_amount", sa.BigInteger, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_holds_amount"),
    )
    op.create_index("ix_holds_card_id", "holds", ["card_id"])
    op.create_table(
        "authorizations",
        sa.Column("trace_id", sa.String(64), primary_key=True),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
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
        sa.Column("settled_at", TS),
        sa.Column("decided_at", TS, nullable=False),
        sa.Column("response", JSONB, nullable=False),
    )
    op.create_index("ix_authorizations_card_id", "authorizations", ["card_id"])
    op.create_table(
        "processed_clearing",
        sa.Column("clearing_id", sa.String(64), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_processed_clearing_trace_id", "processed_clearing", ["trace_id"])
    op.create_table(
        "wallet_tokens",
        sa.Column("token_ref", sa.String(64), primary_key=True),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_wallet_tokens_enrollment_id", "wallet_tokens", ["enrollment_id"])
    create_outbox_table(op)
    create_idempotency_table(op)


def downgrade() -> None:
    for table in (
        "idempotency_keys",
        "outbox",
        "wallet_tokens",
        "processed_clearing",
        "authorizations",
        "holds",
        "cards",
    ):
        op.drop_table(table)
