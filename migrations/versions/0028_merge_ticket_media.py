"""Merge ticket attachment and service/card schema histories."""

revision = "0028_merge_ticket_media"
down_revision = ("0026_ticket_media", "0027_merge_service_cards")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
