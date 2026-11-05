"""Panel admin account plans: renewal packages, a custom renewal, per-plan extra ranges, the
renewal rule, who may buy them and whether they renew."""

import sqlalchemy as sa
from alembic import op

revision = "0039_account_renewal_options"
down_revision = "0038_account_day_price"
branch_labels = None
depends_on = None

RANGES = ("gb_min", "gb_max", "days_min", "days_max")


def upgrade() -> None:
    for name in RANGES:
        op.add_column("account_plans", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "account_plans",
        sa.Column("renew_packages_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("account_plans", sa.Column("renew_packages", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "account_plans",
        sa.Column("renew_custom_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("account_plans", sa.Column("renew_mode", sa.String(length=16), nullable=True))
    op.add_column("account_plans", sa.Column("renewable", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column(
        "account_plans",
        sa.Column(
            "audience",
            sa.Enum("all", "customers", "resellers", name="planaudience", native_enum=False, length=24),
            nullable=False,
            server_default="all",
        ),
    )
    for name in ("renew_packages_enabled", "renew_packages", "renew_custom_enabled", "renewable", "audience"):
        op.alter_column("account_plans", name, server_default=None)


def downgrade() -> None:
    for name in ("audience", "renewable", "renew_mode"):
        op.drop_column("account_plans", name)
    for name in ("renew_custom_enabled", "renew_packages", "renew_packages_enabled", *RANGES):
        op.drop_column("account_plans", name)
