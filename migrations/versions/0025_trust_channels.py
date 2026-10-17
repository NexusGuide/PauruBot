"""Trust channels with their own report kinds, and pausing a forced channel (GRW-07, GRW-08).

The old single ``trust_channel_id`` setting becomes the shop's first row, so every shop keeps
posting exactly what it posted before.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from paurubot.db.base import UTCDateTime

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trust_channels",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(96), server_default="", nullable=False),
        sa.Column("url", sa.String(255), server_default="", nullable=False),
        sa.Column("kinds", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_id", "chat_id"),
    )
    op.create_index("ix_trust_channels_shop_id", "trust_channels", ["shop_id"])
    op.add_column(
        "forced_channels",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.execute(
        "INSERT INTO trust_channels"
        " (shop_id, chat_id, title, url, kinds, is_active, sort, created_at)"
        " SELECT shop_id, (value #>> '{}')::bigint, '', '',"
        ' \'["purchase", "renew"]\'::json, true, 0, now()'
        " FROM shop_settings"
        " WHERE key = 'trust_channel_id' AND json_typeof(value) = 'number'"
    )


def downgrade() -> None:
    op.drop_index("ix_trust_channels_shop_id", table_name="trust_channels")
    op.drop_table("trust_channels")
    op.drop_column("forced_channels", "is_active")
