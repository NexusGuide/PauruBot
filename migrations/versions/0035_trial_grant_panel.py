"""The panel each trial was given on, for per-panel daily trial quotas (F-20)."""

import sqlalchemy as sa
from alembic import op

revision = "0035_trial_grant_panel"
down_revision = "0034_service_sub_client"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trial_grants", sa.Column("panel_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_trial_grants_panel_id_panels"),
        "trial_grants",
        "panels",
        ["panel_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_trial_grants_panel_id"), "trial_grants", ["panel_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_trial_grants_panel_id"), table_name="trial_grants")
    op.drop_constraint(op.f("fk_trial_grants_panel_id_panels"), "trial_grants", type_="foreignkey")
    op.drop_column("trial_grants", "panel_id")
