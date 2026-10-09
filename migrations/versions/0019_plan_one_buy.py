"""one-buy-only products

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-09 00:00:00.000000
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
    op.add_column("plans", sa.Column("one_buy", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("plans", "one_buy")
