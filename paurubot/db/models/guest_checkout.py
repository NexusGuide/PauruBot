"""Durable guest request identities, retained when their purchase is removed/claimed."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from paurubot.db.base import Base, TimestampMixin


class GuestCheckoutIntent(Base, TimestampMixin):
    __tablename__ = "guest_checkout_intents"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    plan_id: Mapped[int] = mapped_column()  # immutable request fingerprint, survives plan deletion
    method: Mapped[str] = mapped_column(String(32))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
