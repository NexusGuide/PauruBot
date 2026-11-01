"""When a service's data ran out, for exhausted-service cleanup (SEC-F15)."""

import sqlalchemy as sa
from alembic import op

import paurubot.db.base

revision = "0033_service_limited_at"
down_revision = "0032_panel_username_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "services", sa.Column("limited_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )
    # Conservative backfill: the real moment is unknown, so cleanup counts from the upgrade.
    # A service is never deleted earlier than the shop's setting allows; at worst a little later.
    op.execute("UPDATE services SET limited_at = now() WHERE status = 'limited'")


def downgrade() -> None:
    op.drop_column("services", "limited_at")
