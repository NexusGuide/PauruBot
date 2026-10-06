"""staff api keys

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06 20:06:31.389558
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_api_keys",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("staff_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("expires_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["staff_id"], ["staff.id"], name=op.f("fk_staff_api_keys_staff_id_staff"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_staff_api_keys_prefix")),
    )
    op.create_index(op.f("ix_staff_api_keys_staff_id"), "staff_api_keys", ["staff_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_staff_api_keys_staff_id"), table_name="staff_api_keys")
    op.drop_table("staff_api_keys")
