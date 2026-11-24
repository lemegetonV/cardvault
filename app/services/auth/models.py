"""auth-service tables (SQLAlchemy Core)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("username", sa.String(64), nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("display_name", sa.String(128), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Keyed by username, not customer_id: unknown usernames are counted too (rule 39).
login_attempts = sa.Table(
    "login_attempts",
    metadata,
    sa.Column("username", sa.String(64), primary_key=True),
    sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Only a SHA-256 of each refresh token is stored. One family per login; rotation adds a
# row to the family, and reuse of a rotated token revokes the whole family (rule 37).
refresh_tokens = sa.Table(
    "refresh_tokens",
    metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("family_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
)

# The RSA signing key is generated on first start and kept in the DB, so no private key
# is committed to the repo and tokens survive a restart. /test/reset keeps it.
signing_keys = sa.Table(
    "signing_keys",
    metadata,
    sa.Column("kid", sa.String(64), primary_key=True),
    sa.Column("private_pem", sa.Text, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)

RESET_TABLES = ["users", "login_attempts", "refresh_tokens"]
