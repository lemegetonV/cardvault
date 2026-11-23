"""profile-service initial schema

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
        "profiles",
        sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(254)),
        sa.Column("email_verified", sa.Boolean, nullable=False),
        sa.Column("phone", sa.String(16)),
        sa.Column("phone_verified", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_profiles_version"),
    )
    op.create_table(
        "otp_state",
        sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.String(8), primary_key=True),
        sa.Column("code_hash", sa.String(64)),
        sa.Column("last_sent_at", TS),
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", TS),
        sa.CheckConstraint("channel IN ('EMAIL','PHONE')", name="ck_otp_channel"),
    )
    create_outbox_table(op)
    create_idempotency_table(op)


def downgrade() -> None:
    for table in ("idempotency_keys", "outbox", "otp_state", "profiles"):
        op.drop_table(table)
