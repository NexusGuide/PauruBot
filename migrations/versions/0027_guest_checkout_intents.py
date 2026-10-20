"""Durable guest checkout identities and cleanup-safe tombstones.

Revision ID: 0027_guest_checkout
Revises: 0026_card_verification
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_guest_checkout"
down_revision = "0026_card_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_checkout_intents",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("shop_id", sa.BigInteger(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("guest_checkout_intents")
