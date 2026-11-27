from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.orm.base import NO_VALUE

from paurubot.db.base import Base, TimestampMixin, UpdatedMixin, str_enum, utcnow
from paurubot.db.models.catalog import Panel, Plan
from paurubot.db.models.enums import (
    LedgerAccountKind,
    OrderKind,
    OrderStatus,
    PaymentPurpose,
    PaymentStatus,
    ServiceStatus,
)
from paurubot.db.models.tenancy import Customer


class PanelUsernameClaim(Base, TimestampMixin):
    """Which order owns a username on a panel before any service exists (SEC-F03).

    Written in the commit that stores the chosen name on the order, so two orders can never
    pick the same account name on a panel, and no order adopts an account it did not create.
    """

    __tablename__ = "panel_username_claims"
    __table_args__ = (UniqueConstraint("panel_id", "username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    username: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)


class Service(Base, UpdatedMixin):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("panel_id", "panel_username"),
        Index("ix_services_customer_status", "customer_id", "status"),
        Index("ix_services_owner_page", "shop_id", "customer_id", "id"),
        Index("ix_services_shop_status_expire", "shop_id", "status", "expire_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="RESTRICT"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    panel_username: Mapped[str] = mapped_column(String(64))
    panel_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    subscription_url: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ServiceStatus] = mapped_column(str_enum(ServiceStatus), default=ServiceStatus.PENDING)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    data_limit: Mapped[int] = mapped_column(default=0)
    used_bytes: Mapped[int] = mapped_column(default=0)
    expire_at: Mapped[datetime | None] = mapped_column()
    online_at: Mapped[datetime | None] = mapped_column()
    synced_at: Mapped[datetime | None] = mapped_column()
    alerts_sent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column()
    delete_reason: Mapped[str | None] = mapped_column(String(255))
    # Why the bot disabled it ("ban", "reseller"), so the matching unban re-enables only those (SVC-06).
    disabled_reason: Mapped[str | None] = mapped_column(String(32))
    staff_note: Mapped[str | None] = mapped_column(String(255))  # seen by staff only
    # Opaque token of the shop-branded subscription link /s/<token> (SVC-08).
    sub_token: Mapped[str | None] = mapped_column(String(48), unique=True)
    # Usage already billed to a metered reseller (TEN-05).
    billed_bytes: Mapped[int] = mapped_column(default=0, server_default="0")
    # addresses last seen on the panel, to tell the customer when a new device pushed one out (JOB-03)
    client_ips: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # The customer's opt-in auto-renewal rule, None when off (F-02; see paurubot.core.auto_renew).
    auto_renew: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # When the data ran out: set as the status becomes LIMITED, cleared when it leaves (SEC-F15).
    # Cleanup ages an exhausted service from here, not from its end date or last row update.
    limited_at: Mapped[datetime | None] = mapped_column()
    # The client app that last fetched the subscription ("v2rayNG 1.8.0"), and when (F-11).
    # Only a recognised app name and version are kept, never the raw User-Agent.
    sub_client: Mapped[str | None] = mapped_column(String(48))
    sub_fetched_at: Mapped[datetime | None] = mapped_column()
    # Expired-service cleanup: when the customer was warned of the coming deletion (cleared as the
    # service comes back to life), and staff holds that keep it, for a while or for good.
    cleanup_warned_at: Mapped[datetime | None] = mapped_column()
    keep_until: Mapped[datetime | None] = mapped_column()
    cleanup_exempt: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    customer: Mapped[Customer] = relationship(lazy="selectin")
    panel: Mapped[Panel] = relationship(lazy="selectin")
    plan: Mapped[Plan | None] = relationship(lazy="selectin")

    @validates("status")
    def _track_limited(self, _key: str, value: ServiceStatus) -> ServiceStatus:
        # Read the previous values without loading them: a validator must never trigger IO.
        loaded = sa_inspect(self).dict
        previous = loaded.get("status", NO_VALUE)
        since = loaded.get("limited_at", NO_VALUE)
        if value not in {ServiceStatus.EXPIRED, ServiceStatus.LIMITED}:
            self.cleanup_warned_at = None  # a later lapse is warned again
        if value != ServiceStatus.LIMITED:
            self.limited_at = None
        elif previous != ServiceStatus.LIMITED or since in (None, NO_VALUE):
            # Unknown history counts from now: later, never earlier, than the real moment.
            self.limited_at = utcnow()
        return value

    @property
    def remaining_bytes(self) -> int | None:
        if not self.data_limit:
            return None
        return max(0, self.data_limit - self.used_bytes)


class ServiceClaim(Base, TimestampMixin):
    """A customer asks to add a service they already have on one of the shop's panels (settings ›
    add a previous service). Staff approve it unless the panel user's note names the customer."""

    __tablename__ = "service_claims"
    __table_args__ = (
        Index("ix_service_claims_shop_status", "shop_id", "status"),
        UniqueConstraint("customer_id", "panel_id", "panel_username"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    panel_username: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(String(128))


class CancellationRequest(Base, UpdatedMixin):
    """A customer's request to cancel a service when staff approval is enabled (SVC-14)."""

    __tablename__ = "cancellation_requests"
    __table_args__ = (
        Index("ix_cancellation_requests_shop_status", "shop_id", "status"),
        Index(
            "uq_cancellation_requests_pending_service",
            "service_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
    reason: Mapped[str | None] = mapped_column(String(500))
    quoted_refund: Mapped[int] = mapped_column(default=0)
    refund_amount: Mapped[int | None] = mapped_column()
    staff_note: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(String(128))


class Order(Base, UpdatedMixin):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_shop_status", "shop_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[OrderKind] = mapped_column(str_enum(OrderKind))
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus), default=OrderStatus.PENDING)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    base_price: Mapped[int] = mapped_column(default=0)
    reseller_discount: Mapped[int] = mapped_column(default=0)
    code_discount: Mapped[int] = mapped_column(default=0)
    amount: Mapped[int] = mapped_column(default=0)
    discount_code_id: Mapped[int | None] = mapped_column(ForeignKey("discount_codes.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime | None] = mapped_column()
    paid_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="bot")

    plan: Mapped[Plan | None] = relationship(lazy="selectin")
    customer: Mapped[Customer] = relationship(lazy="selectin")


class LedgerAccount(Base, TimestampMixin):
    __tablename__ = "ledger_accounts"
    __table_args__ = (UniqueConstraint("shop_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    kind: Mapped[LedgerAccountKind] = mapped_column(str_enum(LedgerAccountKind))
    code: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), unique=True
    )
    balance: Mapped[int] = mapped_column(default=0)
    allow_negative: Mapped[bool] = mapped_column(Boolean, default=False)


class LedgerJournal(Base, TimestampMixin):
    __tablename__ = "ledger_journals"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[int | None] = mapped_column()
    memo: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str | None] = mapped_column(String(64))
    reverses_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_journals.id", ondelete="RESTRICT"))

    entries: Mapped[list[LedgerEntry]] = relationship(lazy="selectin", back_populates="journal")


class LedgerEntry(Base, TimestampMixin):
    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_entries_account_id_desc", "account_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_id: Mapped[int] = mapped_column(ForeignKey("ledger_journals.id", ondelete="RESTRICT"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id", ondelete="RESTRICT"))
    amount: Mapped[int] = mapped_column()
    balance_after: Mapped[int] = mapped_column()

    journal: Mapped[LedgerJournal] = relationship(back_populates="entries", lazy="raise")


class Payment(Base, UpdatedMixin):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_tx_id"),
        Index("ix_payments_shop_status", "shop_id", "status"),
        Index("ix_payments_fulfillment", "status", "fulfilled_at", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    purpose: Mapped[PaymentPurpose] = mapped_column(str_enum(PaymentPurpose))
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[PaymentStatus] = mapped_column(str_enum(PaymentStatus), default=PaymentStatus.CREATED)
    amount: Mapped[int] = mapped_column()
    currency: Mapped[str] = mapped_column(String(8), default="IRT")
    provider_amount: Mapped[int | None] = mapped_column()
    provider_currency: Mapped[str | None] = mapped_column(String(16))
    provider_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    provider_tx_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column()
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_text: Mapped[str | None] = mapped_column(Text)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    receipt_phash: Mapped[str | None] = mapped_column(String(16), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column()
    review_note: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime | None] = mapped_column()
    claim_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    fulfilled_at: Mapped[datetime | None] = mapped_column()
    failure_reason: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    customer: Mapped[Customer] = relationship(lazy="selectin")


class Settlement(Base, TimestampMixin):
    """Money a gateway or card account paid out to the shop's bank (PAY-03)."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column()
    reference: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(String(255))
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    journal_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_journals.id", ondelete="RESTRICT"))
