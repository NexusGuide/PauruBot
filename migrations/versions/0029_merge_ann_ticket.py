"""Merge announcement inbox and ticket attachment schema histories."""

revision = "0029_merge_ann_ticket"
down_revision = ("0028_merge_announcements", "0028_merge_ticket_media")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
