"""service lifecycle and plan options

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06 15:26:21.264203
"""

from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("on_hold", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("plans", sa.Column("panel_options", sa.JSON(), server_default="{}", nullable=False))
    op.add_column("plans", sa.Column("username_prefix", sa.String(length=16), nullable=True))
    op.add_column("plans", sa.Column("addons", sa.JSON(), server_default="{}", nullable=False))
    op.add_column("services", sa.Column("disabled_reason", sa.String(length=32), nullable=True))
    op.add_column("services", sa.Column("sub_token", sa.String(length=48), nullable=True))
    op.create_unique_constraint(op.f("uq_services_sub_token"), "services", ["sub_token"])
    # Give existing services a branded link token.
    bind = op.get_bind()
    services = sa.table("services", sa.column("id", sa.Integer), sa.column("sub_token", sa.String))
    ids = [row[0] for row in bind.execute(sa.select(services.c.id).where(services.c.sub_token.is_(None)))]
    for service_id in ids:
        bind.execute(
            services.update().where(services.c.id == service_id).values(sub_token=secrets.token_urlsafe(24))
        )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_services_sub_token"), "services", type_="unique")
    op.drop_column("services", "sub_token")
    op.drop_column("services", "disabled_reason")
    op.drop_column("plans", "addons")
    op.drop_column("plans", "username_prefix")
    op.drop_column("plans", "panel_options")
    op.drop_column("plans", "on_hold")
