"""reseller hierarchy, billing and limits

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06 15:50:53.897447
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reseller_billing_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("period_end", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("service_hours", sa.BigInteger(), nullable=False),
        sa.Column("active_services", sa.Integer(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("charged", sa.BigInteger(), nullable=False),
        sa.Column("debt_after", sa.BigInteger(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["reseller_profiles.id"],
            name=op.f("fk_reseller_billing_snapshots_profile_id_reseller_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["shops.id"],
            name=op.f("fk_reseller_billing_snapshots_shop_id_shops"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reseller_billing_snapshots")),
    )
    op.create_index(
        "ix_reseller_billing_profile_created",
        "reseller_billing_snapshots",
        ["profile_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reseller_billing_snapshots_shop_id"), "reseller_billing_snapshots", ["shop_id"], unique=False
    )
    op.add_column("reseller_profiles", sa.Column("parent_id", sa.BigInteger(), nullable=True))
    op.add_column("reseller_profiles", sa.Column("discount_override", sa.Integer(), nullable=True))
    op.add_column("reseller_profiles", sa.Column("max_subresellers", sa.Integer(), nullable=True))
    op.add_column(
        "reseller_profiles",
        sa.Column("billing_mode", sa.String(length=16), server_default="prepaid", nullable=False),
    )
    op.add_column(
        "reseller_profiles", sa.Column("gb_price", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "reseller_profiles", sa.Column("hourly_price", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "reseller_profiles", sa.Column("credit_limit", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "reseller_profiles", sa.Column("billing_debt", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "reseller_profiles",
        sa.Column("last_billed_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
    )
    op.add_column("reseller_profiles", sa.Column("low_balance_threshold", sa.BigInteger(), nullable=True))
    op.add_column("reseller_profiles", sa.Column("max_active_services", sa.Integer(), nullable=True))
    op.add_column("reseller_profiles", sa.Column("max_total_gb", sa.Integer(), nullable=True))
    op.add_column(
        "reseller_profiles",
        sa.Column("suspended_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
    )
    op.add_column("reseller_profiles", sa.Column("suspend_reason", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_reseller_profiles_parent_id"), "reseller_profiles", ["parent_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_reseller_profiles_parent_id_reseller_profiles"),
        "reseller_profiles",
        "reseller_profiles",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("services", sa.Column("billed_bytes", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("tickets", sa.Column("source", sa.String(length=16), server_default="bot", nullable=False))


def downgrade() -> None:
    op.drop_column("tickets", "source")
    op.drop_column("services", "billed_bytes")
    op.drop_constraint(
        op.f("fk_reseller_profiles_parent_id_reseller_profiles"), "reseller_profiles", type_="foreignkey"
    )
    op.drop_index(op.f("ix_reseller_profiles_parent_id"), table_name="reseller_profiles")
    op.drop_column("reseller_profiles", "suspend_reason")
    op.drop_column("reseller_profiles", "suspended_at")
    op.drop_column("reseller_profiles", "max_total_gb")
    op.drop_column("reseller_profiles", "max_active_services")
    op.drop_column("reseller_profiles", "low_balance_threshold")
    op.drop_column("reseller_profiles", "last_billed_at")
    op.drop_column("reseller_profiles", "billing_debt")
    op.drop_column("reseller_profiles", "credit_limit")
    op.drop_column("reseller_profiles", "hourly_price")
    op.drop_column("reseller_profiles", "gb_price")
    op.drop_column("reseller_profiles", "billing_mode")
    op.drop_column("reseller_profiles", "max_subresellers")
    op.drop_column("reseller_profiles", "discount_override")
    op.drop_column("reseller_profiles", "parent_id")
    op.drop_index(op.f("ix_reseller_billing_snapshots_shop_id"), table_name="reseller_billing_snapshots")
    op.drop_index("ix_reseller_billing_profile_created", table_name="reseller_billing_snapshots")
    op.drop_table("reseller_billing_snapshots")
