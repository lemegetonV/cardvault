"""profile-service tables (SQLAlchemy Core)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from shared.idempotency import idempotency_table
from shared.outbox import outbox_table

metadata = sa.MetaData()

profiles = sa.Table(
    "profiles",
    metadata,
    sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("email", sa.String(254)),
    sa.Column("email_verified", sa.Boolean, nullable=False),
    sa.Column("phone", sa.String(16)),  # E.164
    sa.Column("phone_verified", sa.Boolean, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),  # optimistic locking (If-Match)
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# One row per customer and channel: OTP cooldown and wrong-code lockout.
otp_state = sa.Table(
    "otp_state",
    metadata,
    sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("channel", sa.String(8), primary_key=True),  # EMAIL | PHONE
    sa.Column("code_hash", sa.String(64)),  # only outside TEST_MODE
    sa.Column("last_sent_at", sa.DateTime(timezone=True)),
    sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
)

outbox = outbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

RESET_TABLES = ["profiles", "otp_state", "outbox", "idempotency_keys"]
