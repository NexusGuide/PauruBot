"""reseller bots

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-07 16:42:53.433819
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("reseller_profile_id", sa.BigInteger(), nullable=True))
    op.add_column("shops", sa.Column("bot_user_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(op.f("uq_shops_bot_user_id"), "shops", ["bot_user_id"])
    op.create_unique_constraint(op.f("uq_shops_reseller_profile_id"), "shops", ["reseller_profile_id"])
    op.create_foreign_key(
        op.f("fk_shops_reseller_profile_id_reseller_profiles"),
        "shops",
        "reseller_profiles",
        ["reseller_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_shops_reseller_profile_id_reseller_profiles"), "shops", type_="foreignkey")
    op.drop_constraint(op.f("uq_shops_reseller_profile_id"), "shops", type_="unique")
    op.drop_constraint(op.f("uq_shops_bot_user_id"), "shops", type_="unique")
    op.drop_column("shops", "bot_user_id")
    op.drop_column("shops", "reseller_profile_id")
