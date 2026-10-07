"""Loyalty tiers, lucky wheel and lotteries (GRW-02, GRW-03, GRW-04)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07 06:19:17.960091
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lotteries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=96), nullable=False),
        sa.Column("prize", sa.String(length=255), nullable=False),
        sa.Column("prize_amount", sa.BigInteger(), nullable=False),
        sa.Column("winners", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("starts_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("ends_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_lotteries_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lotteries")),
    )
    op.create_index(op.f("ix_lotteries_shop_id"), "lotteries", ["shop_id"], unique=False)
    op.create_table(
        "lottery_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("lottery_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("tickets", sa.Integer(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_lottery_entries_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lottery_id"],
            ["lotteries.id"],
            name=op.f("fk_lottery_entries_lottery_id_lotteries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lottery_entries")),
        sa.UniqueConstraint(
            "lottery_id", "customer_id", name=op.f("uq_lottery_entries_lottery_id_customer_id")
        ),
    )
    op.create_table(
        "wheel_spins",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_wheel_spins_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_wheel_spins_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wheel_spins")),
    )
    op.create_index(
        "ix_wheel_spins_customer_created", "wheel_spins", ["customer_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_wheel_spins_shop_id"), "wheel_spins", ["shop_id"], unique=False)
    op.add_column("customers", sa.Column("total_spent", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("customers", sa.Column("tier_percent", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("customers", "tier_percent")
    op.drop_column("customers", "total_spent")
    op.drop_index(op.f("ix_wheel_spins_shop_id"), table_name="wheel_spins")
    op.drop_index("ix_wheel_spins_customer_created", table_name="wheel_spins")
    op.drop_table("wheel_spins")
    op.drop_table("lottery_entries")
    op.drop_index(op.f("ix_lotteries_shop_id"), table_name="lotteries")
    op.drop_table("lotteries")
