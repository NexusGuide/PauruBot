"""Service cancellation requests awaiting staff approval (self_delete_require_approval)."""

import sqlalchemy as sa
from alembic import op

revision = "0049_cancellation_requests"
down_revision = "0048_subcategories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cancellation_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("quoted_refund", sa.BigInteger(), nullable=False),
        sa.Column("refund_amount", sa.BigInteger(), nullable=True),
        sa.Column("staff_note", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_cancellation_requests_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_cancellation_requests_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_cancellation_requests_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cancellation_requests")),
    )
    op.create_index("ix_cancellation_requests_shop_status", "cancellation_requests", ["shop_id", "status"])
    op.create_index(
        "uq_cancellation_requests_pending_service",
        "cancellation_requests",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_cancellation_requests_pending_service", table_name="cancellation_requests")
    op.drop_index("ix_cancellation_requests_shop_status", table_name="cancellation_requests")
    op.drop_table("cancellation_requests")
