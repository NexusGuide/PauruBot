"""panel monitoring and stats

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 19:39:13.259812
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_checks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("checked_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_panel_checks_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panel_checks")),
    )
    op.create_index("ix_panel_checks_panel_checked", "panel_checks", ["panel_id", "checked_at"], unique=False)
    op.add_column("panels", sa.Column("stats", sa.JSON(), server_default="{}", nullable=False))
    op.add_column("panels", sa.Column("stats_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True))
    op.add_column(
        "panels", sa.Column("down_since", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("panels", "down_since")
    op.drop_column("panels", "stats_at")
    op.drop_column("panels", "stats")
    op.drop_index("ix_panel_checks_panel_checked", table_name="panel_checks")
    op.drop_table("panel_checks")
