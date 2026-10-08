"""panel admin accounts

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08 00:03:33.826539
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=96), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("data_gb", sa.Integer(), nullable=False),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("gb_price", sa.BigInteger(), nullable=False),
        sa.Column("user_price", sa.BigInteger(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_account_plans_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_account_plans_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_plans")),
    )
    op.create_index(op.f("ix_account_plans_shop_id"), "account_plans", ["shop_id"], unique=False)
    op.create_table(
        "panel_accounts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_enc", sa.Text(), nullable=False),
        sa.Column("data_limit", sa.BigInteger(), nullable=False),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("expire_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("users_count", sa.Integer(), nullable=False),
        sa.Column("panel_status", sa.String(length=16), nullable=True),
        sa.Column("alerts_sent", sa.JSON(), nullable=False),
        sa.Column("synced_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("updated_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_panel_accounts_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_panel_accounts_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["account_plans.id"],
            name=op.f("fk_panel_accounts_plan_id_account_plans"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_panel_accounts_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panel_accounts")),
        sa.UniqueConstraint("panel_id", "username", name=op.f("uq_panel_accounts_panel_id_username")),
    )
    op.create_index(op.f("ix_panel_accounts_customer_id"), "panel_accounts", ["customer_id"], unique=False)
    op.create_index(op.f("ix_panel_accounts_shop_id"), "panel_accounts", ["shop_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_panel_accounts_shop_id"), table_name="panel_accounts")
    op.drop_index(op.f("ix_panel_accounts_customer_id"), table_name="panel_accounts")
    op.drop_table("panel_accounts")
    op.drop_index(op.f("ix_account_plans_shop_id"), table_name="account_plans")
    op.drop_table("account_plans")
