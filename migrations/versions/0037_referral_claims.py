"""Free configs for counted invites (referral reward config)."""

import sqlalchemy as sa
from alembic import op

import paurubot.db.base

revision = "0037_referral_claims"
down_revision = "0036_ticket_reaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("referral_counted_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "referral_claims",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("invites", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_referral_claims_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_referral_claims_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_referral_claims_order_id_orders"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referral_claims")),
        sa.UniqueConstraint("order_id", name=op.f("uq_referral_claims_order_id")),
    )
    op.create_index(op.f("ix_referral_claims_customer_id"), "referral_claims", ["customer_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_referral_claims_customer_id"), table_name="referral_claims")
    op.drop_table("referral_claims")
    op.drop_column("customers", "referral_counted_at")
