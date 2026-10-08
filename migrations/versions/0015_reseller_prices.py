"""fixed reseller prices per plan and panels hidden from reseller groups

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reseller_groups", sa.Column("hidden_panel_ids", sa.JSON(), server_default="[]", nullable=False)
    )
    op.create_table(
        "reseller_prices",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_reseller_prices_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_reseller_prices_plan_id_plans"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["reseller_groups.id"],
            name=op.f("fk_reseller_prices_group_id_reseller_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["reseller_profiles.id"],
            name=op.f("fk_reseller_prices_profile_id_reseller_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reseller_prices")),
        sa.CheckConstraint(
            "(group_id IS NULL) <> (profile_id IS NULL)", name=op.f("ck_reseller_prices_one_owner")
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_reseller_prices_positive")),
    )
    op.create_index(
        "uq_reseller_prices_plan_group",
        "reseller_prices",
        ["plan_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reseller_prices_plan_profile",
        "reseller_prices",
        ["plan_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("profile_id IS NOT NULL"),
    )
    op.create_index(op.f("ix_reseller_prices_shop_id"), "reseller_prices", ["shop_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_reseller_prices_shop_id"), table_name="reseller_prices")
    op.drop_index("uq_reseller_prices_plan_profile", table_name="reseller_prices")
    op.drop_index("uq_reseller_prices_plan_group", table_name="reseller_prices")
    op.drop_table("reseller_prices")
    op.drop_column("reseller_groups", "hidden_panel_ids")
