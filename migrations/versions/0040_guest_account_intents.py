"""Guest checkout intents name what they buy: a service plan or an admin account plan."""

import sqlalchemy as sa
from alembic import op

revision = "0040_guest_account_intents"
down_revision = "0039_account_renewal_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guest_checkout_intents",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="service"),
    )


def downgrade() -> None:
    op.drop_column("guest_checkout_intents", "kind")
