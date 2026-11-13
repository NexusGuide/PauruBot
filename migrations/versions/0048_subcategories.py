"""Subcategories: a category may sit under another one, one level deep."""

import sqlalchemy as sa
from alembic import op

revision = "0048_subcategories"
down_revision = "0047_cleanup_warning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("parent_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_categories_parent_id_categories"),
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_categories_parent_id_categories"), "categories", type_="foreignkey")
    op.drop_column("categories", "parent_id")
