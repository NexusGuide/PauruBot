"""A dedicated reseller bot on the reseller's own panel: the shop names its admin account."""

import sqlalchemy as sa
from alembic import op

revision = "0041_own_panel_shops"
down_revision = "0040_guest_account_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("own_account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_shops_own_account_id_panel_accounts"),
        "shops",
        "panel_accounts",
        ["own_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(op.f("uq_shops_own_account_id"), "shops", ["own_account_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_shops_own_account_id"), "shops", type_="unique")
    op.drop_constraint(op.f("fk_shops_own_account_id_panel_accounts"), "shops", type_="foreignkey")
    op.drop_column("shops", "own_account_id")
