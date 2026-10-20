"""Merge durable guest checkout and ticket attachment integration histories."""

revision = "0030_merge_guest_ticket"
down_revision = ("0029_merge_ann_guest", "0029_merge_ann_ticket")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
