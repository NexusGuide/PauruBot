"""guest customers

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07 10:44:20.552613
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("guest_token", sa.String(length=48), nullable=True))
    op.create_unique_constraint(op.f("uq_customers_guest_token"), "customers", ["guest_token"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_customers_guest_token"), "customers", type_="unique")
    op.drop_column("customers", "guest_token")
