"""Panel admin accounts: a price per extra day (0: extra days are not sold)."""

import sqlalchemy as sa
from alembic import op

revision = "0038_account_day_price"
down_revision = "0037_referral_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_plans",
        sa.Column("day_price", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.alter_column("account_plans", "day_price", server_default=None)


def downgrade() -> None:
    op.drop_column("account_plans", "day_price")
