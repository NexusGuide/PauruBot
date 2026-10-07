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

from paurubot.core import catalog, pricing, resellers
from paurubot.core import settings as shop_settings
from paurubot.db.base import affected, utcnow
from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountRedemption,
    Job,
    JobStatus,
    Location,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
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


def extra_prices(cfg: dict[str, Any], plan: Plan | None) -> tuple[int, int] | None:
    """Price of one extra GB and one extra day for services of `plan`, or None when not sold (CAT-02)."""
    addons = (plan.addons if plan is not None else None) or {}
    if not cfg["extras_enabled"] or addons.get("extras") is False:
        return None
    gb = addons.get("extra_gb_price")
    day = addons.get("extra_day_price")
    return (
        int(gb) if gb not in (None, "") else int(cfg["extra_gb_price"]),
        int(day) if day not in (None, "") else int(cfg["extra_day_price"]),
    )


def service_extras(cfg: dict[str, Any], service: Service) -> tuple[int | None, int | None]:
    """Unit prices of extra data and extra days for one service; None where it isn't sold.

    The plan decides prices (CAT-02); a panel can switch either one off (ADM-07).
    """
    if service.is_trial:
        return None, None
    prices = extra_prices(cfg, service.plan)
    if prices is None:
        return None, None
    options = (service.panel.options if service.panel is not None else None) or {}
    gb = None if options.get("sell_extra_volume") is False else prices[0]
    days = None if options.get("sell_extra_days") is False else prices[1]
    return gb, days


def custom_limits(cfg: dict[str, Any]) -> dict[str, int]:
    return {
        "min_gb": int(cfg["custom_min_gb"]),
        "max_gb": int(cfg["custom_max_gb"]),
        "min_days": int(cfg["custom_min_days"]),
        "max_days": int(cfg["custom_max_days"]),
    }


def custom_price(cfg: dict[str, Any], gb: int, days: int) -> int:
    """Price of a custom purchase (CAT-03); raises when custom buy is off or out of range."""
    if not cfg["custom_buy_enabled"] or not (cfg["custom_panel_id"] or cfg["custom_location_id"]):
        raise OrderError("custom_unavailable")
    limits = custom_limits(cfg)
    if not (limits["min_gb"] <= gb <= limits["max_gb"]) or not (
        limits["min_days"] <= days <= limits["max_days"]
    ):
        raise OrderError("custom_out_of_range", **limits)
    return pricing.round_price(
        int(cfg["custom_base_price"]) + gb * int(cfg["custom_gb_price"]) + days * int(cfg["custom_day_price"])
    )


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
    custom_gb: int = 0,
    custom_days: int = 0,
    move_to: Panel | None = None,
) -> Draft:
    cfg = await shop_settings.load(session, customer.shop_id)
    params: dict[str, Any] = {}
    if label:
        params["label"] = label[:48]
    add_gb = extra_gb if kind == OrderKind.EXTRA_VOLUME else (custom_gb if plan is None else plan.data_gb)
    try:
        await resellers.check_purchase(session, customer, kind=kind, add_gb=add_gb)
    except resellers.ResellerError as exc:
        raise OrderError(exc.key, **exc.params) from exc
    if kind in {
        OrderKind.RENEW,
        OrderKind.EXTRA_VOLUME,
        OrderKind.EXTRA_DAYS,
        OrderKind.UPGRADE,
        OrderKind.MOVE,
    }:
        if service is None or service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
            raise OrderError("service_not_found")
        if service.is_trial and kind != OrderKind.UPGRADE:
            raise OrderError("trial_not_renewable")
    if kind == OrderKind.PURCHASE:
        if plan is None and (custom_gb or custom_days):
            params["custom"] = {"gb": custom_gb, "days": custom_days}
            return Draft(kind, None, None, params, custom_price(cfg, custom_gb, custom_days))
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
        assert service is not None
        gb_price, _ = service_extras(cfg, service)
        if gb_price is None or extra_gb <= 0 or extra_gb > 10_000:
            raise OrderError("extras_unavailable")
        params["extra_gb"] = extra_gb
        return Draft(kind, None, service, params, extra_gb * gb_price)
    if kind == OrderKind.EXTRA_DAYS:
        assert service is not None
        _, day_price = service_extras(cfg, service)
        if day_price is None or extra_days <= 0 or extra_days > 3650:
            raise OrderError("extras_unavailable")
        params["extra_days"] = extra_days
        return Draft(kind, None, service, params, extra_days * day_price)
    if kind == OrderKind.UPGRADE:
        assert service is not None
        if not cfg["upgrade_enabled"] or plan is None:
            raise OrderError("plan_unavailable")
        if plan.panel_id and plan.panel_id != service.panel_id:
            raise OrderError("plan_wrong_panel")
        credit = 0 if service.is_trial else _remaining_value(service, service.plan)
        params["credit"] = credit
        return Draft(kind, plan, service, params, max(0, plan.price - credit))
    if kind == OrderKind.MOVE:
        assert service is not None
        if not cfg["move_enabled"]:
            raise OrderError("move_unavailable")
        if service.status not in MOVABLE:
            raise OrderError("move_not_now")
        if move_to is None or move_to.shop_id != customer.shop_id or move_to.id == service.panel_id:
            raise OrderError("move_unavailable")
        params["move_to"] = move_to.id
        return Draft(kind, None, service, params, max(0, int(cfg["move_price"] or 0)))
    raise OrderError("plan_unavailable")


MOVABLE = {ServiceStatus.ACTIVE, ServiceStatus.ON_HOLD}


async def move_targets(session: AsyncSession, service: Service) -> list[tuple[Location, Panel]]:
    """Locations a service can move to, each with the panel it would land on (SVC-04)."""
    current = await session.get(Panel, service.panel_id)
    rows = list(
        await session.scalars(
            select(Location)
            .where(Location.shop_id == service.shop_id, Location.is_active.is_(True))
            .order_by(Location.sort, Location.id)
        )
    )
    out: list[tuple[Location, Panel]] = []
    for location in rows:
        if current is not None and current.location_id == location.id:
            continue
        panel = await catalog.pick_panel_for(session, service.shop_id, None, location.id)
        if panel is not None and panel.id != service.panel_id:
            out.append((location, panel))
    return out


async def create(
    session: AsyncSession,
    customer: Customer,
    d: Draft,
    *,
    code: DiscountCode | None = None,
    source: str = "bot",
) -> Order:
    if resellers.metered(customer):
        code = None  # nothing to discount: the order itself is free
    if code is not None:
        await pricing.validate_code(
            session, code, customer, plan_id=d.plan.id if d.plan else None, amount=d.base_price
        )
    q = pricing.quote(d.base_price, customer, code)
    if code is None and resellers.metered(customer):
        # Metered resellers pay for usage or service-hours instead of per order (TEN-05).
        q = pricing.Quote(d.base_price, d.base_price, 0, 0)
        d.params = {**d.params, "metered": True}
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
    await enqueue_provision(session, order)
    await session.flush()
    return order


async def queued_until(session: AsyncSession, order: Order) -> datetime | None:
    """When a renewal should wait for the current period to end (SVC-02), the time it runs."""
    if order.kind != OrderKind.RENEW or not order.service_id:
        return None
    if not await shop_settings.get(session, order.shop_id, "renew_queue_when_active"):
        return None
    service = await session.get(Service, order.service_id)
    if service is None or service.status != ServiceStatus.ACTIVE or service.expire_at is None:
        return None
    if service.data_limit and service.used_bytes >= service.data_limit:
        return None
    if service.expire_at <= utcnow() + timedelta(minutes=5):
        return None
    return service.expire_at


async def enqueue_provision(session: AsyncSession, order: Order) -> None:
    run_at = await queued_until(session, order)
    if run_at is not None:
        order.params = {**(order.params or {}), "queued": True, "queued_until": run_at.isoformat()}
    await queue.enqueue(
        session,
        PROVISION_QUEUE,
        "order",
        {"order_id": order.id},
        shop_id=order.shop_id,
        dedupe_key=f"order:{order.id}:provision",
        run_at=run_at,
        max_attempts=6,
    )


async def has_queued(session: AsyncSession, service_id: int) -> bool:
    found = await session.scalar(
        select(Order.id)
        .where(
            Order.service_id == service_id,
            Order.kind == OrderKind.RENEW,
            Order.status == OrderStatus.PAID,
        )
        .limit(1)
    )
    return found is not None


async def release_queued(session: AsyncSession, service_id: int) -> int:
    """Run waiting renewals of a service now, because its current period ran out early."""
    order_ids = list(
        await session.scalars(
            select(Order.id).where(
                Order.service_id == service_id,
                Order.kind == OrderKind.RENEW,
                Order.status == OrderStatus.PAID,
            )
        )
    )
    if not order_ids:
        return 0
    result = await session.execute(
        update(Job)
        .where(
            Job.dedupe_key.in_([f"order:{oid}:provision" for oid in order_ids]),
            Job.status == JobStatus.QUEUED,
            Job.run_at > utcnow(),
        )
        .values(run_at=utcnow())
    )
    return affected(result)


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
