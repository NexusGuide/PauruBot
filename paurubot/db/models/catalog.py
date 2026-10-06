from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from paurubot.db.base import Base, TimestampMixin, UpdatedMixin, str_enum
from paurubot.db.models.enums import DiscountKind, PanelKind, PlanAudience, UsernameMethod


class Location(Base, TimestampMixin):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(64))
    flag: Mapped[str | None] = mapped_column(String(16))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Panel(Base, UpdatedMixin):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[PanelKind] = mapped_column(str_enum(PanelKind))
    base_url: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tls_cert_pem: Mapped[str | None] = mapped_column(Text)
    allow_private_address: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_secret_enc: Mapped[str | None] = mapped_column(Text)
    capacity: Mapped[int | None] = mapped_column(Integer)
    username_method: Mapped[UsernameMethod] = mapped_column(
        str_enum(UsernameMethod), default=UsernameMethod.TELEGRAM_ID
    )
    username_prefix: Mapped[str] = mapped_column(String(16), default="")
    username_sequence: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_healthy: Mapped[bool | None] = mapped_column(Boolean)
    health_checked_at: Mapped[datetime | None] = mapped_column()
    health_error: Mapped[str | None] = mapped_column(String(255))

    location: Mapped[Location | None] = relationship(lazy="selectin")


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Plan(Base, UpdatedMixin):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(96))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="SET NULL"))
    price: Mapped[int] = mapped_column(default=0)
    days: Mapped[int] = mapped_column(Integer, default=30)
    data_gb: Mapped[int] = mapped_column(Integer, default=0)
    ip_limit: Mapped[int] = mapped_column(Integer, default=0)
    audience: Mapped[PlanAudience] = mapped_column(str_enum(PlanAudience), default=PlanAudience.ALL)
    renewable: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    button_style: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    category: Mapped[Category | None] = relationship(lazy="selectin")
    location: Mapped[Location | None] = relationship(lazy="selectin")
    panel: Mapped[Panel | None] = relationship(lazy="selectin")


class DiscountCode(Base, TimestampMixin):
    __tablename__ = "discount_codes"
    __table_args__ = (UniqueConstraint("shop_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32))
    kind: Mapped[DiscountKind] = mapped_column(str_enum(DiscountKind), default=DiscountKind.PERCENT)
    value: Mapped[int] = mapped_column(default=0)
    max_discount: Mapped[int | None] = mapped_column()
    min_amount: Mapped[int] = mapped_column(default=0)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    per_customer_limit: Mapped[int] = mapped_column(Integer, default=1)
    plan_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    first_purchase_only: Mapped[bool] = mapped_column(Boolean, default=False)
    sticky: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255))


class DiscountRedemption(Base, TimestampMixin):
    __tablename__ = "discount_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    code_id: Mapped[int] = mapped_column(ForeignKey("discount_codes.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), unique=True)
    amount: Mapped[int] = mapped_column(default=0)


class StickyDiscount(Base, TimestampMixin):
    """A code that keeps applying to a customer's future orders until it expires (GRW-05)."""

    __tablename__ = "sticky_discounts"
    __table_args__ = (UniqueConstraint("customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    code_id: Mapped[int] = mapped_column(ForeignKey("discount_codes.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime | None] = mapped_column()


class TrialGrant(Base, TimestampMixin):
    """One row per trial issued; the unique key doubles as the reservation (CAT-04)."""

    __tablename__ = "trial_grants"
    __table_args__ = (UniqueConstraint("shop_id", "customer_id", "generation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    error: Mapped[str | None] = mapped_column(Text)


class GiftCode(Base, TimestampMixin):
    """A code that puts money in the wallet when redeemed (FIN-03)."""

    __tablename__ = "gift_codes"
    __table_args__ = (UniqueConstraint("shop_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column()
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    new_customers_only: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255))


class GiftRedemption(Base, TimestampMixin):
    __tablename__ = "gift_redemptions"
    __table_args__ = (UniqueConstraint("code_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    code_id: Mapped[int] = mapped_column(ForeignKey("gift_codes.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column()
