"""Allow discount codes to target specific panel admin account plans."""

import sqlalchemy as sa
from alembic import op

revision = "0050_discount_account_plans"
down_revision = "0049_cancellation_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discount_codes",
        sa.Column("account_plan_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("discount_codes", "account_plan_ids")
