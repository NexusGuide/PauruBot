"""service client ips

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07 10:38:47.477404
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("client_ips", sa.JSON(), server_default="[]", nullable=False))


def downgrade() -> None:
    op.drop_column("services", "client_ips")
