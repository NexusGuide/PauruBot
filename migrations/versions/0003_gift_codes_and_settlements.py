"""gift codes and settlements

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06 15:10:10.126108
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_codes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("new_customers_only", sa.Boolean(), nullable=False),
        sa.Column("expires_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_gift_codes_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gift_codes")),
        sa.UniqueConstraint("shop_id", "code", name=op.f("uq_gift_codes_shop_id_code")),
    )
    op.create_table(
        "gift_redemptions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("code_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["code_id"],
            ["gift_codes.id"],
            name=op.f("fk_gift_redemptions_code_id_gift_codes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_gift_redemptions_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_gift_redemptions_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gift_redemptions")),
        sa.UniqueConstraint("code_id", "customer_id", name=op.f("uq_gift_redemptions_code_id_customer_id")),
    )
    op.create_index(op.f("ix_gift_redemptions_code_id"), "gift_redemptions", ["code_id"], unique=False)
    op.create_index(
        op.f("ix_gift_redemptions_customer_id"), "gift_redemptions", ["customer_id"], unique=False
    )
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("staff_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["journal_id"],
            ["ledger_journals.id"],
            name=op.f("fk_settlements_journal_id_ledger_journals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_settlements_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"], ["staff.id"], name=op.f("fk_settlements_staff_id_staff"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
    )
    op.create_index(op.f("ix_settlements_shop_id"), "settlements", ["shop_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_settlements_shop_id"), table_name="settlements")
    op.drop_table("settlements")
    op.drop_index(op.f("ix_gift_redemptions_customer_id"), table_name="gift_redemptions")
    op.drop_index(op.f("ix_gift_redemptions_code_id"), table_name="gift_redemptions")
    op.drop_table("gift_redemptions")
    op.drop_table("gift_codes")
