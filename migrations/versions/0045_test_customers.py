"""Test customers (ClockBot's demo customers): marked by staff and left out of sales reports."""

import sqlalchemy as sa
from alembic import op

revision = "0045_test_customers"
down_revision = "0044_service_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("is_test", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("customers", "is_test")
