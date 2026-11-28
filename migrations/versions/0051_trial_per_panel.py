"""Allow one trial per panel per customer and generation (Foxima-style panel trials)."""

import sqlalchemy as sa
from alembic import op

revision = "0051_trial_per_panel"
down_revision = "0050_discount_account_plans"
branch_labels = None
depends_on = None

OLD = "uq_trial_grants_shop_id_customer_id_generation"
NEW = "uq_trial_grants_shop_customer_generation_panel"


def upgrade() -> None:
    op.drop_constraint(OLD, "trial_grants", type_="unique")
    op.create_unique_constraint(
        NEW,
        "trial_grants",
        ["shop_id", "customer_id", "generation", "panel_id"],
    )


def downgrade() -> None:
    # Keep every grant for reporting, but move all but the oldest one in each
    # (shop, customer, generation) group out of the current round so the older
    # three-column uniqueness can be restored.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY shop_id, customer_id, generation
                           ORDER BY id
                       ) AS rn
                FROM trial_grants
                WHERE generation > 0
            )
            UPDATE trial_grants AS t
            SET generation = -t.id
            FROM ranked
            WHERE t.id = ranked.id AND ranked.rn > 1
            """
        )
    )
    op.drop_constraint(NEW, "trial_grants", type_="unique")
    op.create_unique_constraint(
        OLD,
        "trial_grants",
        ["shop_id", "customer_id", "generation"],
    )
