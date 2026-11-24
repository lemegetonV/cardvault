"""enrollment-service tables (SQLAlchemy Core): the wallet enrolment system of record."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from shared.idempotency import idempotency_table
from shared.outbox import outbox_table

metadata = sa.MetaData()
TS = sa.DateTime(timezone=True)

# One row per card (spec 16.1 rule 4). Re-enrolment reuses the row with a higher version.
enrollments = sa.Table(
    "enrollments",
    metadata,
    sa.Column("enrollment_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("card_id", UUID(as_uuid=True), nullable=False, unique=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("operator_ref", sa.String(64)),
    sa.Column("token_ref", sa.String(64)),
    sa.Column("last4", sa.String(4), nullable=False),  # what was last sent to the operator
    sa.Column("expiry", sa.String(7), nullable=False),
    # PENDING | ENROLLED | SUSPENDED | REMOVED | FAILED
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),  # +1 on every push to the operator
    sa.Column("operator_version", sa.Integer, nullable=False, server_default="0"),
    # The last push still waiting for its callback: ENROL | UPDATE | CONTACT | SUSPEND |
    # RESUME | REMOVE. It tells a superseded callback from a current one.
    sa.Column("pending_action", sa.String(16)),
    sa.Column("pending_reason", sa.String(48)),
    sa.Column("status_reason", sa.String(48)),  # why the current status (e.g. CARD_CLOSED)
    sa.Column("failure_reason", sa.Text),
    sa.Column("terms_version", sa.String(32)),
    sa.Column("last_sweep_attempt_on", sa.Date),  # sweep retries FAILED at most once a day
    sa.Column("created_at", TS, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
)

enrollment_history = sa.Table(
    "enrollment_history",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("enrollment_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("from_status", sa.String(16)),
    sa.Column("to_status", sa.String(16), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("operator_version", sa.Integer, nullable=False),
    sa.Column("source", sa.String(32), nullable=False),  # CUSTOMER | CALLBACK | EVENT | ...
    sa.Column("reason", sa.Text),
    sa.Column("correlation_id", sa.String(128)),
    sa.Column("created_at", TS, nullable=False),
)

wallet_prefs = sa.Table(
    "wallet_prefs",
    metadata,
    sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("opted_out", sa.Boolean, nullable=False),
    sa.Column("terms_version", sa.String(32)),
    sa.Column("updated_at", TS, nullable=False),
)

# Per-card removal by the customer (spec 16.2 rule 12): the sweep never re-enrols it.
wallet_card_prefs = sa.Table(
    "wallet_card_prefs",
    metadata,
    sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("opted_out", sa.Boolean, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
)

processed_callbacks = sa.Table(
    "processed_callbacks",
    metadata,
    sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("enrollment_id", UUID(as_uuid=True)),
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("received_at", TS, nullable=False),
)

processed_events = sa.Table(
    "processed_events",
    metadata,
    sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("topic", sa.String(64), nullable=False),
    sa.Column("processed_at", TS, nullable=False),
)

# Local projections (spec 16.3 rule 21), fed by card.lifecycle and contact.updated.
card_projection = sa.Table(
    "card_projection",
    metadata,
    sa.Column("card_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("customer_id", UUID(as_uuid=True), nullable=False, index=True),
    sa.Column("product", sa.String(16), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("last4", sa.String(4), nullable=False),
    sa.Column("expiry", sa.String(7), nullable=False),
    sa.Column("last_used_at", TS),
    sa.Column("updated_at", TS, nullable=False),
)

contact_projection = sa.Table(
    "contact_projection",
    metadata,
    sa.Column("customer_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("email", sa.String(254)),
    sa.Column("email_verified", sa.Boolean, nullable=False),
    sa.Column("phone", sa.String(16)),
    sa.Column("phone_verified", sa.Boolean, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
)

# Reconciliation against the operator's daily enrolment file (spec 6.3).
reconciliation_runs = sa.Table(
    "reconciliation_runs",
    metadata,
    sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("file_date", sa.Date, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # COMPLETED | FAILED
    sa.Column("sor_records", sa.Integer, nullable=False, server_default="0"),
    sa.Column("file_records", sa.Integer, nullable=False, server_default="0"),
    sa.Column("skipped_in_flight", sa.Integer, nullable=False, server_default="0"),
    sa.Column("finding_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
    sa.Column("started_at", TS, nullable=False),
    sa.Column("finished_at", TS),
)

reconciliation_findings = sa.Table(
    "reconciliation_findings",
    metadata,
    sa.Column("finding_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False, index=True),
    # MISSING_AT_OPERATOR | MISSING_IN_SOR | STATUS_MISMATCH | DATA_MISMATCH
    sa.Column("type", sa.String(24), nullable=False),
    sa.Column("enrollment_id", UUID(as_uuid=True)),
    sa.Column("card_id", UUID(as_uuid=True)),
    sa.Column("operator_ref", sa.String(64)),
    sa.Column("sor_status", sa.String(16)),
    sa.Column("operator_status", sa.String(16)),
    sa.Column("details", JSONB, nullable=False),
)

outbox = outbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

RESET_TABLES = [
    "enrollments",
    "enrollment_history",
    "wallet_prefs",
    "wallet_card_prefs",
    "processed_callbacks",
    "processed_events",
    "card_projection",
    "contact_projection",
    "outbox",
    "idempotency_keys",
    "reconciliation_runs",
    "reconciliation_findings",
]
