"""Persistent shop announcements and per-customer read receipts.

Revision ID: 0026_announcements (distinct from hosted-payment attempt revisions).
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from paurubot.db.base import UTCDateTime

revision = "0026_announcements"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_staff_id", sa.BigInteger(), nullable=True),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("starts_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("expires_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_id", "request_key"),
        sa.CheckConstraint("kind IN ('operational', 'marketing')", name="announcement_kind"),
        sa.CheckConstraint("expires_at IS NULL OR expires_at > starts_at", name="announcement_window"),
    )
    op.create_index("ix_announcements_shop_id_desc", "announcements", ["shop_id", "id"])
    op.create_table(
        "announcement_reads",
        sa.Column("announcement_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("seen_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("read_at", UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["announcement_id"], ["announcements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("announcement_id", "customer_id"),
    )
    op.create_index("ix_announcement_reads_customer_id", "announcement_reads", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_announcement_reads_customer_id", table_name="announcement_reads")
    op.drop_table("announcement_reads")
    op.drop_index("ix_announcements_shop_id_desc", table_name="announcements")
    op.drop_table("announcements")
