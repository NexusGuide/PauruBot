"""Lotteries: purchase points, ranked prizes (wallet, GB, days, text), how winners are picked and
what customers see of the standings."""

import sqlalchemy as sa
from alembic import op

revision = "0043_lottery_prizes"
down_revision = "0042_account_plan_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lotteries", sa.Column("prizes", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("lotteries", sa.Column("pick", sa.String(16), nullable=False, server_default="random"))
    op.add_column("lotteries", sa.Column("points_per", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("lotteries", sa.Column("board", sa.String(16), nullable=False, server_default="none"))


def downgrade() -> None:
    for column in ("board", "points_per", "pick", "prizes"):
        op.drop_column("lotteries", column)
