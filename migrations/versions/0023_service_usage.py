"""Observed service usage history (F-06), stacked after F-15's 0022.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from paurubot.db.base import UTCDateTime

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_usage",
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("bucket_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("observed_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("period_key", sa.String(64), nullable=False),
        sa.Column("break_before", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("service_id", "bucket_at"),
    )
    op.create_index("ix_service_usage_bucket", "service_usage", ["bucket_at"])


def downgrade() -> None:
    op.drop_index("ix_service_usage_bucket", table_name="service_usage")
    op.drop_table("service_usage")
