"""auto-renewal rule per service

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13 04:30:00.000000
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
    op.add_column("services", sa.Column("auto_renew", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "auto_renew")
