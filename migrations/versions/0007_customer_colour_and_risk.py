"""customer colour and risk

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06 19:52:43.518939
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("color", sa.String(length=12), nullable=True))
    op.add_column("customers", sa.Column("risk", sa.String(length=8), server_default="none", nullable=False))


def downgrade() -> None:
    op.drop_column("customers", "risk")
    op.drop_column("customers", "color")
