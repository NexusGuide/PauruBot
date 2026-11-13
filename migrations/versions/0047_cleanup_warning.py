"""Expired-service cleanup warns first, and staff can keep a service out of it (PGClockBot parity)."""

import sqlalchemy as sa
from alembic import op

revision = "0047_cleanup_warning"
down_revision = "0046_shop_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("cleanup_warned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("services", sa.Column("keep_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "services",
        sa.Column("cleanup_exempt", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("services", "cleanup_exempt")
    op.drop_column("services", "keep_until")
    op.drop_column("services", "cleanup_warned_at")
