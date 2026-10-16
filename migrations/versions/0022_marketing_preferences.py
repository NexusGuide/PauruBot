"""Independent customer marketing preference (F-15).

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from paurubot.db.base import UTCDateTime

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve existing delivery behaviour; this default is NOT evidence of consent.
    op.add_column(
        "customers", sa.Column("marketing_opt_out", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("customers", sa.Column("marketing_changed_at", UTCDateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("marketing_change_source", sa.String(16), nullable=True))
    op.add_column("broadcasts", sa.Column("skipped", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("broadcasts", "skipped")
    op.drop_column("customers", "marketing_change_source")
    op.drop_column("customers", "marketing_changed_at")
    op.drop_column("customers", "marketing_opt_out")
