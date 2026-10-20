"""Persistent shop announcements, independent of computed service alerts and Telegram jobs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from paurubot.db.base import Base, TimestampMixin


class Announcement(Base, TimestampMixin):
    __tablename__ = "announcements"
    __table_args__ = (
        UniqueConstraint("shop_id", "request_key"),
        CheckConstraint("kind IN ('operational', 'marketing')", name="announcement_kind"),
        CheckConstraint("expires_at IS NULL OR expires_at > starts_at", name="announcement_window"),
        Index("ix_announcements_shop_id_desc", "shop_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    created_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    request_key: Mapped[str] = mapped_column(String(36))
    fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    starts_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"

    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    seen_at: Mapped[datetime] = mapped_column()
    read_at: Mapped[datetime | None] = mapped_column()
