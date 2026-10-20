"""Durable Mini App card registration replay; no change to staff review or payments.

Revision ID: 0026_card_verification
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_card_verification"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verified_cards", sa.Column("client_intent_key", sa.String(64), nullable=True))
    op.add_column("verified_cards", sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_verified_cards_intent", "verified_cards", ["shop_id", "customer_id", "client_intent_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_verified_cards_intent", "verified_cards", type_="unique")
    op.drop_column("verified_cards", "request_fingerprint")
    op.drop_column("verified_cards", "client_intent_key")
