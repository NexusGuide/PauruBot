"""Subscription-link domains (ClockBot parity B4): hosts a shop's links may use, with their check."""

import sqlalchemy as sa
from alembic import op

revision = "0046_shop_domains"
down_revision = "0045_test_customers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shop_domains",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("for_links", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_issuer", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_shop_domains_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shop_domains")),
        sa.UniqueConstraint("host", name=op.f("uq_shop_domains_host")),
    )
    op.create_index(op.f("ix_shop_domains_shop_id"), "shop_domains", ["shop_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_shop_domains_shop_id"), table_name="shop_domains")
    op.drop_table("shop_domains")
