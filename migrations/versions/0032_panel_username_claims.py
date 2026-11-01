"""Durable panel username claims per order (SEC-F03)."""

import sqlalchemy as sa
from alembic import op

revision = "0032_panel_username_claims"
down_revision = "0031_manual_national"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_username_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["panel_id"],
            ["panels.id"],
            name=op.f("fk_panel_username_claims_panel_id_panels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_panel_username_claims_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panel_username_claims")),
        sa.UniqueConstraint("panel_id", "username", name=op.f("uq_panel_username_claims_panel_id_username")),
    )
    op.create_index(op.f("ix_panel_username_claims_order_id"), "panel_username_claims", ["order_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_panel_username_claims_order_id"), table_name="panel_username_claims")
    op.drop_table("panel_username_claims")
