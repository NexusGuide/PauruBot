"""panels hidden from one customer

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-08 02:00:17.734080
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("hidden_panel_ids", sa.JSON(), server_default="[]", nullable=False))


def downgrade() -> None:
    op.drop_column("customers", "hidden_panel_ids")
