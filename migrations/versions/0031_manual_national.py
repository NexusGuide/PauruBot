"""Manual inventory metadata and explicit national-network stock routing."""

import sqlalchemy as sa
from alembic import op

revision = "0031_manual_national"
down_revision = "0030_merge_guest_ticket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "panels", sa.Column("national_mode", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("plans", sa.Column("national_stock_plan_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_plans_national_stock", "plans", "plans", ["national_stock_plan_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_plans_national_stock_plan_id", "plans", ["national_stock_plan_id"])
    op.add_column("stock_items", sa.Column("name", sa.String(96), nullable=True))
    op.add_column("stock_items", sa.Column("content_bytes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("stock_items", sa.Column("part_count", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    for column in ("part_count", "content_bytes", "name"):
        op.drop_column("stock_items", column)
    op.drop_index("ix_plans_national_stock_plan_id", table_name="plans")
    op.drop_constraint("fk_plans_national_stock", "plans", type_="foreignkey")
    op.drop_column("plans", "national_stock_plan_id")
    op.drop_column("panels", "national_mode")
