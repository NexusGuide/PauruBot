"""broadcast pause, resume, schedule and delivery breakdown (F-16)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13 02:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("blocked", sa.Integer(), server_default="0", nullable=False))
    op.add_column("broadcasts", sa.Column("deleted", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "broadcasts", sa.Column("scheduled_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )
    op.add_column("broadcasts", sa.Column("run", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("broadcasts", "run")
    op.drop_column("broadcasts", "scheduled_at")
    op.drop_column("broadcasts", "deleted")
    op.drop_column("broadcasts", "blocked")
