"""Ready-made config stock (CAT-05)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07 06:00:58.051088
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_enc", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("customer_id", sa.BigInteger(), nullable=True),
        sa.Column("sold_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_stock_items_customer_id_customers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_stock_items_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_stock_items_plan_id_plans"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_stock_items_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_items")),
        sa.UniqueConstraint("shop_id", "digest", name=op.f("uq_stock_items_shop_id_digest")),
    )
    op.create_index(op.f("ix_stock_items_customer_id"), "stock_items", ["customer_id"], unique=False)
    op.create_index("ix_stock_items_plan_status", "stock_items", ["plan_id", "status"], unique=False)
    op.create_index(op.f("ix_stock_items_shop_id"), "stock_items", ["shop_id"], unique=False)
    op.add_column(
        "plans", sa.Column("delivery", sa.String(length=16), server_default="panel", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("plans", "delivery")
    op.drop_index(op.f("ix_stock_items_shop_id"), table_name="stock_items")
    op.drop_index("ix_stock_items_plan_status", table_name="stock_items")
    op.drop_index(op.f("ix_stock_items_customer_id"), table_name="stock_items")
    op.drop_table("stock_items")
