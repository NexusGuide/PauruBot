"""Hourly last-observed panel counters, not estimated per-hour traffic (F-06)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from paurubot.db.base import Base


class ServiceUsage(Base):
    __tablename__ = "service_usage"
    __table_args__ = (Index("ix_service_usage_bucket", "bucket_at"),)

    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    bucket_at: Mapped[datetime] = mapped_column(primary_key=True)
    observed_at: Mapped[datetime] = mapped_column()
    used_bytes: Mapped[int] = mapped_column()
    period_key: Mapped[str] = mapped_column(String(64))
    break_before: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source: Mapped[str] = mapped_column(String(16))
