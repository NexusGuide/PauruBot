"""Owner-scoped service keyset pagination index.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_services_owner_page", "services", ["shop_id", "customer_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_services_owner_page", table_name="services")
