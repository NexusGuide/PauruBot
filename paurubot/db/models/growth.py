"""Lucky wheel spins and lotteries (GRW-03, GRW-04)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from paurubot.db.base import Base, TimestampMixin


class WheelSpin(Base, TimestampMixin):
    __tablename__ = "wheel_spins"
    __table_args__ = (Index("ix_wheel_spins_customer_created", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column(default=0)
    code: Mapped[str | None] = mapped_column(String(32))


class Lottery(Base, TimestampMixin):
    __tablename__ = "lotteries"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(96))
    prize: Mapped[str] = mapped_column(String(255))
    prize_amount: Mapped[int] = mapped_column(default=0)
    winners: Mapped[int] = mapped_column(Integer, default=1)
    # "join": customers press a button; "purchases": every completed order in the period is a ticket;
    # "points": every `points_per` toman paid in the period is a point.
    mode: Mapped[str] = mapped_column(String(16), default="join")
    # Ranked prizes: [{kind: wallet|gb|days|text, value, label}], first item for first place.
    # Empty on lotteries made before ranked prizes: every winner gets `prize` / `prize_amount`.
    prizes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    # "random": weighted by tickets/points; "top": the highest scores win, in order.
    pick: Mapped[str] = mapped_column(String(16), default="random", server_default="random")
    points_per: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # What customers see in the bot: "none", "self" (their score and rank) or "top" (also the top 5).
    board: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    starts_at: Mapped[datetime] = mapped_column()
    ends_at: Mapped[datetime] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="open")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LotteryEntry(Base, TimestampMixin):
    __tablename__ = "lottery_entries"
    __table_args__ = (UniqueConstraint("lottery_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lottery_id: Mapped[int] = mapped_column(ForeignKey("lotteries.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    tickets: Mapped[int] = mapped_column(Integer, default=1)


class BusinessConnection(Base, TimestampMixin):
    """A Telegram Business account that added the bot (BOT-07). Only staff accounts are acted on."""

    __tablename__ = "business_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Telegram's connection id
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column()  # the business account owner
    trusted: Mapped[bool] = mapped_column(default=False)  # a staff member's account
    enabled: Mapped[bool] = mapped_column(default=True)
    can_reply: Mapped[bool] = mapped_column(default=False)
    replies: Mapped[int] = mapped_column(Integer, default=0)
