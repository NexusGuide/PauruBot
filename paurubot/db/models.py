from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from paurubot.db.base import Base, TimestampMixin, utcnow


def _enum(cls: type[enum.Enum]) -> Enum:
    return Enum(cls, native_enum=False, length=24, values_callable=lambda e: [m.value for m in e])


class PanelKind(enum.StrEnum):
    PASARGUARD = "pasarguard"
    MARZBAN = "marzban"
    XUI = "xui"


class ServiceStatus(enum.StrEnum):
    ACTIVE = "active"
    LIMITED = "limited"
    EXPIRED = "expired"
    DISABLED = "disabled"
    DELETED = "deleted"


class OrderKind(enum.StrEnum):
    PURCHASE = "purchase"
    RENEW = "renew"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class PaymentMethod(enum.StrEnum):
    CARD = "card"
    ZARINPAL = "zarinpal"
    STARS = "stars"
    ADMIN = "admin"


class PaymentStatus(enum.StrEnum):
    WAITING_RECEIPT = "waiting_receipt"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TxKind(enum.StrEnum):
    TOPUP = "topup"
    PURCHASE = "purchase"
    RENEW = "renew"
    REFUND = "refund"
    REFERRAL = "referral"
    ADMIN = "admin"


class ResellerRequestStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ResellerGroup(Base, TimestampMixin):
    __tablename__ = "reseller_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    min_topup: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    balance: Mapped[int] = mapped_column(default=0)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    test_used: Mapped[bool] = mapped_column(Boolean, default=False)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    reseller_group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reseller_groups.id", ondelete="SET NULL")
    )
    phone: Mapped[str | None] = mapped_column(String(20))
    last_seen_at: Mapped[datetime | None] = mapped_column(default=utcnow)

    reseller_group: Mapped[ResellerGroup | None] = relationship(lazy="joined")

    @property
    def is_reseller(self) -> bool:
        return self.reseller_group_id is not None

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or str(self.id)


class Panel(Base, TimestampMixin):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[PanelKind] = mapped_column(_enum(PanelKind))
    base_url: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    capacity: Mapped[int | None] = mapped_column(Integer)
    username_prefix: Mapped[str] = mapped_column(String(16), default="")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Plan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(96))
    panel_id: Mapped[int] = mapped_column(Integer, ForeignKey("panels.id", ondelete="CASCADE"))
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL")
    )
    price: Mapped[int] = mapped_column(default=0)
    days: Mapped[int] = mapped_column(Integer, default=30)
    data_gb: Mapped[int] = mapped_column(Integer, default=0)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    resellers_only: Mapped[bool] = mapped_column(Boolean, default=False)

    panel: Mapped[Panel] = relationship(lazy="joined")
    category: Mapped[Category | None] = relationship(lazy="joined")


class Service(Base, TimestampMixin):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("panel_id", "panel_username"),
        Index("ix_services_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    panel_id: Mapped[int] = mapped_column(Integer, ForeignKey("panels.id", ondelete="CASCADE"))
    plan_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("plans.id", ondelete="SET NULL"))
    panel_username: Mapped[str] = mapped_column(String(64))
    panel_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    subscription_url: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ServiceStatus] = mapped_column(_enum(ServiceStatus), default=ServiceStatus.ACTIVE)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    data_limit: Mapped[int] = mapped_column(default=0)
    used_bytes: Mapped[int] = mapped_column(default=0)
    expire_at: Mapped[datetime | None] = mapped_column()
    synced_at: Mapped[datetime | None] = mapped_column()
    warned_volume: Mapped[bool] = mapped_column(Boolean, default=False)
    warned_expiry: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(lazy="joined")
    panel: Mapped[Panel] = relationship(lazy="joined")
    plan: Mapped[Plan | None] = relationship(lazy="joined")


class DiscountCode(Base, TimestampMixin):
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    plan_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("plans.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[OrderKind] = mapped_column(_enum(OrderKind))
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.PENDING)
    plan_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("plans.id", ondelete="SET NULL"))
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL")
    )
    base_price: Mapped[int] = mapped_column(default=0)
    amount: Mapped[int] = mapped_column(default=0)
    discount_code_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("discount_codes.id", ondelete="SET NULL")
    )
    label: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column()

    plan: Mapped[Plan | None] = relationship(lazy="joined")


class DiscountUse(Base, TimestampMixin):
    __tablename__ = "discount_uses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_id: Mapped[int] = mapped_column(Integer, ForeignKey("discount_codes.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id", ondelete="SET NULL"))


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    method: Mapped[PaymentMethod] = mapped_column(_enum(PaymentMethod))
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus))
    amount: Mapped[int] = mapped_column()
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id", ondelete="SET NULL"))
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_text: Mapped[str | None] = mapped_column(Text)
    gateway_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    reviewed_by: Mapped[int | None] = mapped_column()
    reviewed_at: Mapped[datetime | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="joined")


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[TxKind] = mapped_column(_enum(TxKind))
    amount: Mapped[int] = mapped_column()
    balance_after: Mapped[int] = mapped_column()
    reference: Mapped[str | None] = mapped_column(String(128))


class ResellerRequest(Base, TimestampMixin):
    __tablename__ = "reseller_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ResellerRequestStatus] = mapped_column(
        _enum(ResellerRequestStatus), default=ResellerRequestStatus.PENDING
    )

    user: Mapped[User] = relationship(lazy="joined")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class WebAdmin(Base, TimestampMixin):
    __tablename__ = "web_admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    last_login_at: Mapped[datetime | None] = mapped_column()
