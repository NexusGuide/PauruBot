from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from paurubot.db.base import Base, TimestampMixin, UpdatedMixin, str_enum
from paurubot.db.models.enums import ApplicationStatus, ShopStatus, StaffRole


class Shop(Base, UpdatedMixin):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(96))
    currency: Mapped[str] = mapped_column(String(8), default="IRT")
    default_locale: Mapped[str] = mapped_column(String(8), default="fa")
    status: Mapped[ShopStatus] = mapped_column(str_enum(ShopStatus), default=ShopStatus.ACTIVE)
    bot_token_enc: Mapped[str | None] = mapped_column(Text)
    bot_username: Mapped[str | None] = mapped_column(String(64))


class ShopSetting(Base):
    __tablename__ = "shop_settings"

    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("shop_id", "telegram_id"),
        Index("ix_customers_shop_created", "shop_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    telegram_id: Mapped[int] = mapped_column()
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    locale: Mapped[str | None] = mapped_column(String(8))
    phone: Mapped[str | None] = mapped_column(String(20))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(255))
    trial_generation: Mapped[int | None] = mapped_column(Integer)
    terms_accepted_version: Mapped[int | None] = mapped_column(Integer)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    # Staff markers (BOT-08): a colour to spot someone at a glance and a risk level that tightens rules.
    color: Mapped[str | None] = mapped_column(String(12))
    risk: Mapped[str] = mapped_column(String(8), default="none", server_default="none")
    last_seen_at: Mapped[datetime | None] = mapped_column()
    blocked_bot_at: Mapped[datetime | None] = mapped_column()

    reseller_profile: Mapped[ResellerProfile | None] = relationship(
        back_populates="customer", foreign_keys="ResellerProfile.customer_id", uselist=False, lazy="selectin"
    )

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or str(self.telegram_id)

    @property
    def is_reseller(self) -> bool:
        profile = self.reseller_profile
        return profile is not None and profile.is_current


class Staff(Base, TimestampMixin):
    __tablename__ = "staff"
    __table_args__ = (UniqueConstraint("shop_id", "username"), UniqueConstraint("shop_id", "telegram_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    role: Mapped[StaffRole] = mapped_column(str_enum(StaffRole))
    username: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    telegram_id: Mapped[int | None] = mapped_column()
    display_name: Mapped[str | None] = mapped_column(String(96))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    reseller_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("reseller_profiles.id", ondelete="CASCADE")
    )
    last_login_at: Mapped[datetime | None] = mapped_column()


class StaffApiKey(Base, TimestampMixin):
    """A personal API key: only its hash is stored and the secret is shown once (SEC-03)."""

    __tablename__ = "staff_api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()


class ResellerGroup(Base, TimestampMixin):
    __tablename__ = "reseller_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(64))
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    min_topup: Mapped[int] = mapped_column(default=0)
    max_active_services: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResellerProfile(Base, TimestampMixin):
    __tablename__ = "reseller_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("reseller_groups.id", ondelete="SET NULL"))
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column()
    max_purchases_per_day: Mapped[int | None] = mapped_column(Integer)
    # Level-2 resellers hang under a level-1 reseller, who sees their sales and earns the margin (TEN-01).
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("reseller_profiles.id", ondelete="SET NULL"), index=True
    )
    discount_override: Mapped[int | None] = mapped_column(Integer)
    max_subresellers: Mapped[int | None] = mapped_column(Integer)
    # Billing (TEN-05): prepaid pays per order; usage pays per GB used; hourly per active service-hour.
    billing_mode: Mapped[str] = mapped_column(String(16), default="prepaid", server_default="prepaid")
    gb_price: Mapped[int] = mapped_column(default=0, server_default="0")
    hourly_price: Mapped[int] = mapped_column(default=0, server_default="0")
    credit_limit: Mapped[int] = mapped_column(default=0, server_default="0")
    billing_debt: Mapped[int] = mapped_column(default=0, server_default="0")
    last_billed_at: Mapped[datetime | None] = mapped_column()
    # Limits and warnings (TEN-06, TEN-07)
    low_balance_threshold: Mapped[int | None] = mapped_column()
    max_active_services: Mapped[int | None] = mapped_column(Integer)
    max_total_gb: Mapped[int | None] = mapped_column(Integer)
    suspended_at: Mapped[datetime | None] = mapped_column()
    suspend_reason: Mapped[str | None] = mapped_column(String(32))

    customer: Mapped[Customer] = relationship(
        back_populates="reseller_profile", foreign_keys=[customer_id], lazy="raise"
    )
    group: Mapped[ResellerGroup | None] = relationship(lazy="selectin")

    @property
    def discount_percent(self) -> int:
        if self.discount_override is not None:
            return max(0, min(100, int(self.discount_override)))
        if self.group is None or not self.group.is_active:
            return 0
        return max(0, min(100, int(self.group.discount_percent)))

    @property
    def is_metered(self) -> bool:
        return self.billing_mode in {"usage", "hourly"}

    @property
    def is_current(self) -> bool:
        """Active and not past its expiry date (TEN-08)."""
        if not self.is_active:
            return False
        return self.expires_at is None or self.expires_at > datetime.now(UTC)


class ResellerBillingSnapshot(Base, TimestampMixin):
    """One metered billing run for a reseller: what was used and what it cost (TEN-05)."""

    __tablename__ = "reseller_billing_snapshots"
    __table_args__ = (Index("ix_reseller_billing_profile_created", "profile_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("reseller_profiles.id", ondelete="CASCADE"))
    period_start: Mapped[datetime] = mapped_column()
    period_end: Mapped[datetime] = mapped_column()
    used_bytes: Mapped[int] = mapped_column(default=0)
    service_hours: Mapped[int] = mapped_column(default=0)
    active_services: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[int] = mapped_column(default=0)
    charged: Mapped[int] = mapped_column(default=0)
    debt_after: Mapped[int] = mapped_column(default=0)


class ResellerApplication(Base, TimestampMixin):
    __tablename__ = "reseller_applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ApplicationStatus] = mapped_column(
        str_enum(ApplicationStatus), default=ApplicationStatus.PENDING
    )
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column()
