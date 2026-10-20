"""Merge durable guest checkout with the integrated service/card migration chain.

Revision ID: 0028_merge_guest_checkout
Revises: 0027_guest_checkout, 0027_merge_service_cards
"""

revision = "0028_merge_guest_checkout"
down_revision = ("0027_guest_checkout", "0027_merge_service_cards")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
