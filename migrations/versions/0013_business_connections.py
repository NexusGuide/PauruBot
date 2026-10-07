"""business connections

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-07 11:15:09.858835
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("trusted", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("can_reply", sa.Boolean(), nullable=False),
        sa.Column("replies", sa.Integer(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_business_connections_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_connections")),
    )
    op.create_index(
        op.f("ix_business_connections_shop_id"), "business_connections", ["shop_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_business_connections_shop_id"), table_name="business_connections")
    op.drop_table("business_connections")
