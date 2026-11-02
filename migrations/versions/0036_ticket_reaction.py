"""The customer's reaction to a staff reply in a ticket (F-22)."""

import sqlalchemy as sa
from alembic import op

revision = "0036_ticket_reaction"
down_revision = "0035_trial_grant_panel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_messages", sa.Column("reaction", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_messages", "reaction")
