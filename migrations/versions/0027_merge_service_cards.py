"""Merge service pagination and card verification migration branches.

Revision ID: 0027_merge_service_cards
Revises: 0026, 0026_card_verification
"""

revision = "0027_merge_service_cards"
down_revision = ("0026", "0026_card_verification")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
