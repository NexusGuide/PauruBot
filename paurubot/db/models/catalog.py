from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    national_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    health_checked_at: Mapped[datetime | None] = mapped_column()
    health_error: Mapped[str | None] = mapped_column(String(255))
    # Latest resource, quota and node snapshot from the panel (PNL-02, PNL-13).
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    stats_at: Mapped[datetime | None] = mapped_column()
    down_since: Mapped[datetime | None] = mapped_column()

    location: Mapped[Location | None] = relationship(lazy="selectin")


class PanelCheck(Base):
    """One uptime probe of a panel (PNL-12)."""

    __tablename__ = "panel_checks"
    __table_args__ = (Index("ix_panel_checks_panel_checked", "panel_id", "checked_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    checked_at: Mapped[datetime] = mapped_column()
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(255))


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # One level of subcategories (e.g. VOLUME › PER-USER): the category this one sits under.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))


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
    # Starts counting days on the first connection instead of at purchase (SVC-03).
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Overrides of the panel's groups / inbounds / flow for services of this plan (PNL-14).
    panel_options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    username_prefix: Mapped[str | None] = mapped_column(String(16))
    # Per-plan add-on prices for extra data / days; empty keys fall back to the shop settings.
    addons: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")

    # "panel": services are created on a panel; "stock": a ready-made config from the stock (CAT-05).
    delivery: Mapped[str] = mapped_column(String(16), default="panel", server_default="panel")
    # How a renewal treats what's left (SVC-13); None follows the shop's setting.
    renew_mode: Mapped[str | None] = mapped_column(String(16))
    national_stock_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )

    category: Mapped[Category | None] = relationship(lazy="selectin")
    location: Mapped[Location | None] = relationship(lazy="selectin")
    panel: Mapped[Panel | None] = relationship(lazy="selectin")


class AccountPlan(Base, UpdatedMixin):
    """A panel admin account for sale (TEN-14): a PasarGuard admin with a role, a traffic quota, a
    user cap and a period. The buyer creates and manages its own users in the panel."""

    __tablename__ = "account_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    # The shop's categories, shared with config plans; a buyer picks one first (e.g. Tunnel / Direct).
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(96))
    description: Mapped[str | None] = mapped_column(Text)
    role_id: Mapped[int] = mapped_column(Integer)
    data_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0: no traffic quota
    max_users: Mapped[int | None] = mapped_column(Integer)  # None: the role's own limit
    days: Mapped[int] = mapped_column(Integer, default=30)  # 0: no end date
    price: Mapped[int] = mapped_column(default=0)
    gb_price: Mapped[int] = mapped_column(default=0)  # one extra GB; 0: not sold
    user_price: Mapped[int] = mapped_column(default=0)  # one extra user slot; 0: not sold
    day_price: Mapped[int] = mapped_column(default=0)  # one extra day; 0: not sold
    # How much extra GB / days one order may buy, and a custom renewal's range; None: the shop's.
    gb_min: Mapped[int | None] = mapped_column(Integer)
    gb_max: Mapped[int | None] = mapped_column(Integer)
    days_min: Mapped[int | None] = mapped_column(Integer)
    days_max: Mapped[int | None] = mapped_column(Integer)
    # Renewal: ready-made packages [{"days", "gb", "price"}] and/or a custom one the buyer sizes
    # (days x day_price + GB x gb_price). With neither, a renewal repeats the plan.
    renew_packages_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    renew_packages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    renew_custom_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # What a renewal does to the time and traffic left (the four modes of SVC-13); None keeps
    # the original rule: time added to what is left, a fresh quota, usage reset.
    renew_mode: Mapped[str | None] = mapped_column(String(16))
    renewable: Mapped[bool] = mapped_column(Boolean, default=True)
    # Who may buy it: everyone, ordinary customers only or resellers only.
    audience: Mapped[PlanAudience] = mapped_column(str_enum(PlanAudience), default=PlanAudience.ALL)
    # Off by default (TEN-14): discount codes and the reseller group's discount on this plan.
    discounts_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reseller_discount: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    panel: Mapped[Panel | None] = relationship(lazy="selectin")


class PanelAccount(Base, UpdatedMixin):
    """A panel admin account sold to a customer (TEN-14)."""

    __tablename__ = "panel_accounts"
    __table_args__ = (UniqueConstraint("panel_id", "username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("account_plans.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str] = mapped_column(Text)
    data_limit: Mapped[int] = mapped_column(default=0)  # bytes; 0: no quota
    max_users: Mapped[int | None] = mapped_column(Integer)
    expire_at: Mapped[datetime | None] = mapped_column()
    # active | paused (by its owner) | expired | deleted
    status: Mapped[str] = mapped_column(String(16), default="active")
    used_bytes: Mapped[int] = mapped_column(default=0)
    users_count: Mapped[int] = mapped_column(Integer, default=0)
    panel_status: Mapped[str | None] = mapped_column(String(16))
    alerts_sent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text)  # staff only
    # The owner's auto-renewal from the wallet: {"package": i} or {} for the plan itself, plus
    # the worker's bookkeeping (last_order_id, last_at, short, stopped). None: off.
    auto_renew: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    panel: Mapped[Panel | None] = relationship(lazy="selectin")
    plan: Mapped[AccountPlan | None] = relationship(lazy="selectin")


class StockItem(Base, TimestampMixin):
    """One ready-made config sold as-is (CAT-05). The payload is stored encrypted."""

    __tablename__ = "stock_items"
    __table_args__ = (
        Index("ix_stock_items_plan_status", "plan_id", "status"),
        UniqueConstraint("shop_id", "digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(String(96))
    content_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    part_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    payload_enc: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="available")
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    sold_at: Mapped[datetime | None] = mapped_column()


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
    # A private code only this customer can use (GRW-10).
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))


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
    # The panel the trial was given on, for each panel's own daily trial quota (F-20).
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="SET NULL"), index=True)
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


class ReferralClaim(Base, TimestampMixin):
    """A free config a customer took for their counted invites (see paurubot.core.referral_rewards).

    `invites` is what the claim used up. A claim whose order failed, was canceled or refunded
    gives them back: only claims with a live order count as spent.
    """

    __tablename__ = "referral_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    invites: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), unique=True)


class VerifiedCard(Base, TimestampMixin):
    """A bank card a customer registered before paying card to card (PAY-18)."""

    __tablename__ = "verified_cards"
    __table_args__ = (
        Index("ix_verified_cards_customer", "customer_id", "status"),
        UniqueConstraint("shop_id", "customer_id", "client_intent_key", name="uq_verified_cards_intent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    last4: Mapped[str] = mapped_column(String(4))
    masked: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64), index=True)  # the full number, hashed
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | approved | rejected
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column()
    client_intent_key: Mapped[str | None] = mapped_column(String(64))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
