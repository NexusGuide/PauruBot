"""Order state machine (purchase, renew, extras, upgrade).

pending ──pay──▶ paid ──worker──▶ provisioning ──▶ completed
   │                                   └─fail(final)─▶ failed ─refund─▶ refunded
   └──timeout/cancel──▶ canceled
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.base import affected, utcnow
from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountRedemption,
    Order,
    OrderKind,
    OrderStatus,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.ledger.posting import InsufficientFunds
from paurubot.worker import queue

PROVISION_QUEUE = "provision"


class OrderError(Exception):
    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(slots=True)
class Draft:
    kind: OrderKind
    plan: Plan | None
    service: Service | None
    params: dict[str, Any]
    base_price: int


def _remaining_value(service: Service, plan: Plan | None) -> int:
    """Unused share of the current plan's price, used as credit when upgrading."""
    if plan is None or not plan.days or service.expire_at is None:
        return 0
    seconds_left = (service.expire_at - datetime.now(UTC)).total_seconds()
    if seconds_left <= 0:
        return 0
    fraction = min(1.0, seconds_left / (plan.days * 86400))
    return int(plan.price * fraction)


async def draft(
    session: AsyncSession,
    customer: Customer,
    kind: OrderKind,
    *,
    plan: Plan | None = None,
    service: Service | None = None,
    extra_gb: int = 0,
    extra_days: int = 0,
    label: str | None = None,
) -> Draft:
    cfg = await shop_settings.load(session, customer.shop_id)
    params: dict[str, Any] = {}
    if label:
        params["label"] = label[:48]
    if kind in {OrderKind.RENEW, OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS, OrderKind.UPGRADE}:
        if service is None or service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
            raise OrderError("service_not_found")
        if service.is_trial and kind != OrderKind.UPGRADE:
            raise OrderError("trial_not_renewable")
    if kind == OrderKind.PURCHASE:
        if plan is None:
            raise OrderError("plan_unavailable")
        return Draft(kind, plan, None, params, plan.price)
    if kind == OrderKind.RENEW:
        assert service is not None
        if plan is None or not plan.renewable:
            raise OrderError("plan_unavailable")
        if plan.panel_id and plan.panel_id != service.panel_id:
            raise OrderError("plan_wrong_panel")
        return Draft(kind, plan, service, params, plan.price)
    if kind == OrderKind.EXTRA_VOLUME:
        if not cfg["extras_enabled"] or extra_gb <= 0 or extra_gb > 10_000:
            raise OrderError("extras_unavailable")
        params["extra_gb"] = extra_gb
        return Draft(kind, None, service, params, extra_gb * int(cfg["extra_gb_price"]))
    if kind == OrderKind.EXTRA_DAYS:
        if not cfg["extras_enabled"] or extra_days <= 0 or extra_days > 3650:
            raise OrderError("extras_unavailable")
        params["extra_days"] = extra_days
        return Draft(kind, None, service, params, extra_days * int(cfg["extra_day_price"]))
    if kind == OrderKind.UPGRADE:
        assert service is not None
        if not cfg["upgrade_enabled"] or plan is None:
            raise OrderError("plan_unavailable")
        if plan.panel_id and plan.panel_id != service.panel_id:
            raise OrderError("plan_wrong_panel")
        credit = 0 if service.is_trial else _remaining_value(service, service.plan)
        params["credit"] = credit
        return Draft(kind, plan, service, params, max(0, plan.price - credit))
    raise OrderError("plan_unavailable")


async def create(
    session: AsyncSession,
    customer: Customer,
    d: Draft,
    *,
    code: DiscountCode | None = None,
    source: str = "bot",
) -> Order:
    if code is not None:
        await pricing.validate_code(
            session, code, customer, plan_id=d.plan.id if d.plan else None, amount=d.base_price
        )
    q = pricing.quote(d.base_price, customer, code)
    minutes = int(await shop_settings.get(session, customer.shop_id, "order_expire_minutes") or 60)
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=d.kind,
        plan_id=d.plan.id if d.plan else None,
        service_id=d.service.id if d.service else None,
        params=d.params,
        base_price=q.base_price,
        reseller_discount=q.reseller_discount,
        code_discount=q.code_discount,
        amount=q.amount,
        discount_code_id=code.id if code else None,
        expires_at=utcnow() + timedelta(minutes=minutes),
        source=source,
    )
    session.add(order)
    await session.flush()
    return order


async def lock_pending(session: AsyncSession, order_id: int) -> Order | None:
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    if order is None or order.status != OrderStatus.PENDING:
        return None
    if order.expires_at and order.expires_at <= utcnow():
        order.status = OrderStatus.CANCELED
        order.error = "expired"
        return None
    return order


async def _redeem(session: AsyncSession, order: Order) -> None:
    if not order.discount_code_id:
        return
    result = await session.execute(
        update(DiscountCode)
        .where(
            DiscountCode.id == order.discount_code_id,
            (DiscountCode.max_uses.is_(None)) | (DiscountCode.used_count < DiscountCode.max_uses),
        )
        .values(used_count=DiscountCode.used_count + 1)
    )
    if not affected(result):
        raise OrderError("discount_used_up")
    session.add(
        DiscountRedemption(
            shop_id=order.shop_id,
            code_id=order.discount_code_id,
            customer_id=order.customer_id,
            order_id=order.id,
            amount=order.code_discount,
        )
    )
    code = await session.get(DiscountCode, order.discount_code_id)
    if code is not None and code.sticky:
        customer = await session.get(Customer, order.customer_id)
        if customer is not None:
            await pricing.attach_sticky(session, customer, code)


async def settle_from_wallet(session: AsyncSession, order_id: int) -> Order:
    """Charge the wallet, mark the order paid and enqueue provisioning — all in the caller's transaction."""
    order = await lock_pending(session, order_id)
    if order is None:
        raise OrderError("order_expired")
    try:
        await wallet.charge(session, order.shop_id, order.customer_id, order.amount, order_id=order.id)
    except InsufficientFunds as exc:
        balance = await wallet_balance(session, order.shop_id, order.customer_id)
        raise OrderError("insufficient_balance", balance=balance, required=order.amount) from exc
    await _redeem(session, order)
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await queue.enqueue(
        session,
        PROVISION_QUEUE,
        "order",
        {"order_id": order.id},
        shop_id=order.shop_id,
        dedupe_key=f"order:{order.id}:provision",
        max_attempts=6,
    )
    await session.flush()
    return order


async def cancel(session: AsyncSession, order_id: int, customer_id: int | None = None) -> bool:
    stmt = update(Order).where(Order.id == order_id, Order.status == OrderStatus.PENDING)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    result = await session.execute(stmt.values(status=OrderStatus.CANCELED, error="canceled"))
    return bool(affected(result))


async def expire_stale(session: AsyncSession) -> int:
    result = await session.execute(
        update(Order)
        .where(Order.status == OrderStatus.PENDING, Order.expires_at < utcnow())
        .values(status=OrderStatus.CANCELED, error="expired")
    )
    return affected(result)


async def refund_failed(session: AsyncSession, order: Order, reason: str, actor: str = "system") -> None:
    """Return the money of an order that could not be fulfilled."""
    if order.status in {OrderStatus.REFUNDED, OrderStatus.COMPLETED}:
        return
    await wallet.refund(
        session, order.shop_id, order.customer_id, order.amount, order_id=order.id, actor=actor
    )
    order.status = OrderStatus.REFUNDED
    order.error = reason[:500]
    await session.execute(delete(DiscountRedemption).where(DiscountRedemption.order_id == order.id))
    if order.discount_code_id:
        await session.execute(
            update(DiscountCode)
            .where(DiscountCode.id == order.discount_code_id, DiscountCode.used_count > 0)
            .values(used_count=DiscountCode.used_count - 1)
        )
