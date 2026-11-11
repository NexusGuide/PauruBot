"""Customers add a service they already have on the shop's panels (service claims), and the
separate reseller purchase terms each customer accepted."""

import sqlalchemy as sa
from alembic import op

revision = "0044_service_claims"
down_revision = "0043_lottery_prizes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("reseller_terms_version", sa.Integer(), nullable=True))
    op.create_table(
        "service_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_username", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_service_claims_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_service_claims_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_service_claims_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_service_claims_service_id_services"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_claims")),
        sa.UniqueConstraint(
            "customer_id",
            "panel_id",
            "panel_username",
            name=op.f("uq_service_claims_customer_id_panel_id_panel_username"),
        ),
    )
    op.create_index("ix_service_claims_shop_status", "service_claims", ["shop_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_service_claims_shop_status", table_name="service_claims")
    op.drop_table("service_claims")
    op.drop_column("customers", "reseller_terms_version")
