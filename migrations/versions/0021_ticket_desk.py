"""ticket departments, priorities, internal notes and canned replies (F-14)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14 01:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import paurubot.db.base  # noqa: F401

# Follows 0020 (broadcast controls, F-16); merge PR #41 before this PR.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_departments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("log_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("log_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_ticket_departments_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_departments")),
    )
    op.create_index(op.f("ix_ticket_departments_shop_id"), "ticket_departments", ["shop_id"], unique=False)
    op.create_table(
        "canned_replies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("created_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["ticket_departments.id"],
            name=op.f("fk_canned_replies_department_id_ticket_departments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_canned_replies_shop_id_shops"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canned_replies")),
    )
    op.create_index(op.f("ix_canned_replies_shop_id"), "canned_replies", ["shop_id"], unique=False)
    op.add_column("tickets", sa.Column("department_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_tickets_department_id_ticket_departments"),
        "tickets",
        "ticket_departments",
        ["department_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "tickets", sa.Column("priority", sa.String(length=8), server_default="normal", nullable=False)
    )
    op.add_column(
        "tickets", sa.Column("last_customer_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tickets", sa.Column("last_staff_at", paurubot.db.base.UTCDateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ticket_messages", sa.Column("is_internal", sa.Boolean(), server_default="false", nullable=False)
    )
    # Tickets from before the desk: who wrote last is read off their messages.
    op.execute(
        "UPDATE tickets SET last_customer_at = (SELECT MAX(m.created_at) FROM ticket_messages m"
        " WHERE m.ticket_id = tickets.id AND m.author_type <> 'staff')"
    )
    op.execute(
        "UPDATE tickets SET last_staff_at = (SELECT MAX(m.created_at) FROM ticket_messages m"
        " WHERE m.ticket_id = tickets.id AND m.author_type = 'staff')"
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "is_internal")
    op.drop_column("tickets", "last_staff_at")
    op.drop_column("tickets", "last_customer_at")
    op.drop_column("tickets", "priority")
    op.drop_constraint(op.f("fk_tickets_department_id_ticket_departments"), "tickets", type_="foreignkey")
    op.drop_column("tickets", "department_id")
    op.drop_index(op.f("ix_canned_replies_shop_id"), table_name="canned_replies")
    op.drop_table("canned_replies")
    op.drop_index(op.f("ix_ticket_departments_shop_id"), table_name="ticket_departments")
    op.drop_table("ticket_departments")
