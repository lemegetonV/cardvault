"""enrollment-service initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from shared.idempotency import create_idempotency_table
from shared.outbox import create_outbox_table

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "enrollments",
        sa.Column("enrollment_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("operator_ref", sa.String(64)),
        sa.Column("token_ref", sa.String(64)),
        sa.Column("last4", sa.String(4), nullable=False),  # what was last sent to the operator
        sa.Column("expiry", sa.String(7), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),  # +1 on every push to the operator
        sa.Column("operator_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pending_action", sa.String(16)),
        sa.Column("pending_reason", sa.String(48)),
        sa.Column("status_reason", sa.String(48)),  # why the current status (e.g. CARD_CLOSED)
        sa.Column("failure_reason", sa.Text),
        sa.Column("terms_version", sa.String(32)),
        sa.Column("last_sweep_attempt_on", sa.Date),  # sweep retries FAILED at most once a day
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "enrollment_history",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("enrollment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("operator_version", sa.Integer, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),  # CUSTOMER | CALLBACK | EVENT | ...
        sa.Column("reason", sa.Text),
        sa.Column("correlation_id", sa.String(128)),
        sa.Column("created_at", TS, nullable=False),
    )

    op.create_table(
        "wallet_prefs",
        sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("opted_out", sa.Boolean, nullable=False),
        sa.Column("terms_version", sa.String(32)),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "wallet_card_prefs",
        sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("opted_out", sa.Boolean, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "processed_callbacks",
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("enrollment_id", UUID(as_uuid=True)),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("received_at", TS, nullable=False),
    )

    op.create_table(
        "processed_events",
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("processed_at", TS, nullable=False),
    )

    op.create_table(
        "card_projection",
        sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("expiry", sa.String(7), nullable=False),
        sa.Column("last_used_at", TS),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "contact_projection",
        sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(254)),
        sa.Column("email_verified", sa.Boolean, nullable=False),
        sa.Column("phone", sa.String(16)),
        sa.Column("phone_verified", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_enrollments_customer", "enrollments", ["customer_id"])
    op.create_index("ix_history_enrollment", "enrollment_history", ["enrollment_id"])
    op.create_index("ix_card_prefs_customer", "wallet_card_prefs", ["customer_id"])
    op.create_index("ix_card_projection_customer", "card_projection", ["customer_id"])
    op.create_check_constraint(
        "ck_enrollments_status",
        "enrollments",
        "status IN ('PENDING','ENROLLED','SUSPENDED','REMOVED','FAILED')",
    )
    create_outbox_table(op)
    create_idempotency_table(op)


def downgrade() -> None:
    for table in (
        "idempotency_keys",
        "outbox",
        "contact_projection",
        "card_projection",
        "processed_events",
        "processed_callbacks",
        "wallet_card_prefs",
        "wallet_prefs",
        "enrollment_history",
        "enrollments",
    ):
        op.drop_table(table)
