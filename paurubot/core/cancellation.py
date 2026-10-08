"""A customer deleting its own service, with part of the unused value back in the wallet (SVC-14)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Service, ServiceStatus
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
    days = service.plan.days if service.plan is not None else 0
    value = paid.amount * unused_share(service, days) * percent / 100
    return pricing.round_price(value) if value >= 100 else 0


async def cancel(session: AsyncSession, customer: Customer, service: Service) -> int:
    """Delete the service from its panel and refund its unused value once. Returns the refund."""
    from paurubot.provisioning import lifecycle

    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg["self_delete_enabled"]:
        raise CancelError("self_delete_off")
    if service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
        raise CancelError("service_not_found")
    amount = await quote(session, customer, service)
    await lifecycle.delete_service(session, service, "deleted by customer")
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
    return amount
