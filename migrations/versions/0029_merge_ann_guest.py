"""Merge announcement and guest checkout schema histories."""

revision = "0029_merge_ann_guest"
down_revision = ("0028_merge_announcements", "0028_merge_guest_checkout")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
