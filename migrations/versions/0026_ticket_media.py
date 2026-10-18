"""Owned ticket media, replay keys and minimal service context.

Revision ID: 0026_ticket_media
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_ticket_media"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("service_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_tickets_service_id_services", "tickets", "services", ["service_id"], ["id"], ondelete="SET NULL"
    )
    op.add_column("tickets", sa.Column("service_context", sa.JSON(), nullable=True))
    for table in ("tickets", "ticket_messages"):
        op.add_column(table, sa.Column("request_key", sa.String(64), nullable=True))
        op.add_column(table, sa.Column("request_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_ticket_customer_request", "tickets", ["customer_id", "request_key"])
    op.create_unique_constraint(
        "uq_ticket_message_request",
        "ticket_messages",
        ["ticket_id", "author_type", "author_id", "request_key"],
    )
    for column in (
        sa.Column("attachment_data", sa.LargeBinary(), nullable=True),
        sa.Column("attachment_size", sa.Integer(), nullable=True),
        sa.Column("attachment_mime", sa.String(32), nullable=True),
        sa.Column("attachment_name", sa.String(64), nullable=True),
        sa.Column("attachment_expired", sa.Boolean(), nullable=False, server_default="false"),
    ):
        op.add_column("ticket_messages", column)


def downgrade() -> None:
    for column in (
        "attachment_expired",
        "attachment_name",
        "attachment_mime",
        "attachment_size",
        "attachment_data",
    ):
        op.drop_column("ticket_messages", column)
    op.drop_constraint("uq_ticket_message_request", "ticket_messages", type_="unique")
    op.drop_constraint("uq_ticket_customer_request", "tickets", type_="unique")
    for table in ("ticket_messages", "tickets"):
        op.drop_column(table, "request_hash")
        op.drop_column(table, "request_key")
    op.drop_constraint("fk_tickets_service_id_services", "tickets", type_="foreignkey")
    op.drop_column("tickets", "service_context")
    op.drop_column("tickets", "service_id")
