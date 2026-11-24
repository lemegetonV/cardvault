"""reconciliation runs and findings

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("file_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sor_records", sa.Integer, nullable=False, server_default="0"),
        sa.Column("file_records", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_in_flight", sa.Integer, nullable=False, server_default="0"),
        sa.Column("finding_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.CheckConstraint("status IN ('COMPLETED','FAILED')", name="ck_recon_run_status"),
    )
    op.create_table(
        "reconciliation_findings",
        sa.Column("finding_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(24), nullable=False),
        sa.Column("enrollment_id", UUID(as_uuid=True)),
        sa.Column("card_id", UUID(as_uuid=True)),
        sa.Column("operator_ref", sa.String(64)),
        sa.Column("sor_status", sa.String(16)),
        sa.Column("operator_status", sa.String(16)),
        sa.Column("details", JSONB, nullable=False),
        sa.CheckConstraint(
            "type IN ('MISSING_AT_OPERATOR','MISSING_IN_SOR','STATUS_MISMATCH','DATA_MISMATCH')",
            name="ck_recon_finding_type",
        ),
    )
    op.create_index("ix_recon_findings_run", "reconciliation_findings", ["run_id"])


def downgrade() -> None:
    op.drop_table("reconciliation_findings")
    op.drop_table("reconciliation_runs")
