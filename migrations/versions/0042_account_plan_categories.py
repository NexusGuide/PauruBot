"""Panel admin account plans sit in the shop's categories, like config plans."""

import sqlalchemy as sa
from alembic import op

revision = "0042_account_plan_categories"
down_revision = "0041_own_panel_shops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_plans", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_account_plans_category_id_categories"),
        "account_plans",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_account_plans_category_id_categories"), "account_plans", type_="foreignkey")
    op.drop_column("account_plans", "category_id")
