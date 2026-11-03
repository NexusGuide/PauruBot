"""A customer deleting its own service, with part of the unused value back in the wallet (SVC-14)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Plan, Service, ServiceStatus
from paurubot.ledger.accounts import system_account, wallet_account
from paurubot.ledger.posting import transfer


class CancelError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def last_payment(session: AsyncSession, service: Service) -> Order | None:
    """The last paid purchase or renewal of this service."""
    return await session.scalar(
        select(Order)
        .where(
            Order.service_id == service.id,
            Order.status == OrderStatus.COMPLETED,
            Order.kind.in_([OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE]),
            Order.amount > 0,
        )
        .order_by(Order.completed_at.desc(), Order.id.desc())
        .limit(1)
    )


def unused_share(service: Service, days: int) -> float:
    """What's left of the period and of the data, whichever is smaller (0..1)."""
    shares: list[float] = []
    if days > 0 and service.expire_at is not None:
        seconds = (service.expire_at - datetime.now(UTC)).total_seconds()
        shares.append(max(0.0, min(1.0, seconds / (days * 86400))))
    if service.data_limit:
        left = max(0, int(service.data_limit) - int(service.used_bytes))
        shares.append(max(0.0, min(1.0, left / int(service.data_limit))))
    return min(shares) if shares else 0.0


async def period_days(session: AsyncSession, service: Service, paid: Order) -> int:
    """The period the last payment bought: its plan's, a custom order's own, else the service plan's.

    A custom service has no plan; without the order's own days its unused time counted as all of it.
    """
    plan = await session.get(Plan, paid.plan_id) if paid.plan_id else None
    if plan is not None and plan.days:
        return int(plan.days)
    custom = (paid.params or {}).get("custom")
    if isinstance(custom, dict) and int(custom.get("days") or 0) > 0:
        return int(custom["days"])
    return int(service.plan.days) if service.plan is not None and service.plan.days else 0


async def quote(session: AsyncSession, customer: Customer, service: Service) -> int:
    """What deleting this service gives back, after the shop's percentage."""
    cfg = await shop_settings.load(session, customer.shop_id)
    percent = max(0, min(100, int(cfg["self_delete_refund_percent"] or 0)))
    profile = customer.reseller_profile
    if service.is_trial or percent == 0 or (profile is not None and profile.is_metered):
        return 0  # metered resellers were never charged up front
    paid = await last_payment(session, service)
    if paid is None:
        return 0
    days = await period_days(session, service, paid)
    value = paid.amount * unused_share(service, days) * percent / 100
    return pricing.round_price(value) if value >= 100 else 0


async def cancel(session: AsyncSession, customer: Customer, service: Service) -> int:
    """Delete the service from its panel and refund its unused value once. Returns the refund."""
    from paurubot.provisioning import lifecycle

    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg["self_delete_enabled"]:
        raise CancelError("self_delete_off")
    try:
        # Own the service and price it from what the panel shows now (SEC-F09): a stale copy
        # could refund unused data the customer had already used, or race a renewal.
        async with lifecycle.exclusive(session, service):
            if service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
                raise CancelError("service_not_found")
            await lifecycle.refresh(session, service)
            if service.status == ServiceStatus.DELETED:
                raise CancelError("service_not_found")
            amount = await quote(session, customer, service)
            refunded = await last_payment(session, service) if amount > 0 else None
            await lifecycle.delete_service(session, service, "deleted by customer")
    except lifecycle.ServiceBusy as exc:
        raise CancelError("service_busy") from exc
    if amount > 0:
        source = await system_account(session, customer.shop_id, "revenue")
        target = await wallet_account(session, customer.shop_id, customer.id)
        await transfer(
            session,
            shop_id=customer.shop_id,
            source_id=source.id,
            target_id=target.id,
            amount=amount,
            kind="refund",
            idempotency_key=f"service:{service.id}:cancel",
            reference_type="service",
            reference_id=service.id,
            memo="service deleted by the customer",
        )
        if refunded is not None and (refunded.params or {}).get("referral_counted"):
            # A purchase taken back for a refund no longer counts as an invite toward the
            # referrer's reward config; otherwise buying and deleting at once farms configs.
            customer.referral_counted_at = None
    return amount
