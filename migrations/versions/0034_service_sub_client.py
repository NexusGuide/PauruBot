"""The client app that last fetched a service's subscription, and when (F-11)."""

import sqlalchemy as sa
from alembic import op

import paurubot.db.base

revision = "0034_service_sub_client"
down_revision = "0033_service_limited_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("sub_client", sa.String(48), nullable=True))
    op.add_column(
        "services", sa.Column("sub_fetched_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("services", "sub_fetched_at")
    op.drop_column("services", "sub_client")
