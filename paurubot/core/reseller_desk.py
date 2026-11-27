"""What a reseller works with day to day from the bot (TEN-13): figures, finding a service it sold,
services about to run out, renewing many at once and a sales report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import catalog, orders, pricing
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerEntry,
    LedgerJournal,
    Order,
    OrderKind,
    OrderStatus,
    Plan,
    Service,
    ServiceStatus,
    Shop,
)
from paurubot.db.search import ESCAPE, contains
from paurubot.panels import GB

SEARCH_LIMIT = 20
RENEW_LIMIT = 50
LIVE = (ServiceStatus.ACTIVE, ServiceStatus.ON_HOLD, ServiceStatus.LIMITED, ServiceStatus.EXPIRED)


@dataclass(slots=True)
class Figures:
    active: int
    expiring: int
    month_orders: int
    month_amount: int


async def _bot_shop_id(session: AsyncSession, customer: Customer) -> int | None:
    profile = customer.reseller_profile
    if profile is None:
        return None
    return await session.scalar(select(Shop.id).where(Shop.reseller_profile_id == profile.id).limit(1))


async def figures(session: AsyncSession, customer: Customer, days: int = 3) -> Figures:
    now = utcnow()
    bot_shop = await _bot_shop_id(session, customer)
    svc_owner = Service.customer_id == customer.id
    ord_owner = Order.customer_id == customer.id
    if bot_shop is not None:
        svc_owner = or_(svc_owner, Service.shop_id == bot_shop)
        ord_owner = or_(ord_owner, Order.shop_id == bot_shop)
    active = await session.scalar(
        select(func.count(Service.id)).where(svc_owner, Service.status == ServiceStatus.ACTIVE)
    )
    month = await session.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
            ord_owner,
            Order.status == OrderStatus.COMPLETED,
            Order.completed_at >= now - timedelta(days=30),
        )
    )
    count, amount = month.one()
    return Figures(
        int(active or 0),
        len(await expiring(session, customer, days)),
        int(count or 0),
        int(amount or 0),
    )


async def search(session: AsyncSession, customer: Customer, text: str) -> list[Service]:
    """The reseller's services whose label or panel username contains `text`."""
    query = text.strip().lstrip("@")[:48]
    if not query:
        return []
    like = contains(query)
    rows = await session.scalars(
        select(Service)
        .where(
            Service.customer_id == customer.id,
            Service.status != ServiceStatus.DELETED,
            or_(Service.label.ilike(like, escape=ESCAPE), Service.panel_username.ilike(like, escape=ESCAPE)),
        )
        .order_by(Service.id.desc())
        .limit(SEARCH_LIMIT)
    )
    return list(rows)


async def expiring(session: AsyncSession, customer: Customer, days: int = 3) -> list[Service]:
    """Services that end within `days` days or have used 90% of their data, soonest first."""
    soon = utcnow() + timedelta(days=days)
    rows = await session.scalars(
        select(Service)
        .where(
            Service.customer_id == customer.id,
            Service.status.in_(LIVE),
            Service.is_trial.is_(False),
            or_(
                (Service.expire_at.is_not(None)) & (Service.expire_at <= soon),
                (Service.data_limit > 0) & (Service.used_bytes * 10 >= Service.data_limit * 9),
            ),
        )
        .order_by(Service.expire_at.asc().nulls_last(), Service.id)
        .limit(RENEW_LIMIT)
    )
    return list(rows)


@dataclass(slots=True)
class RenewPlan:
    """Services that can be renewed with their own plan, what each costs, and those that can't."""

    items: list[tuple[Service, Plan, int]] = field(default_factory=list)
    skipped: list[Service] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(price for _, _, price in self.items)


async def renew_plan(session: AsyncSession, customer: Customer, services: list[Service]) -> RenewPlan:
    out = RenewPlan()
    for service in services[:RENEW_LIMIT]:
        plan = (
            await catalog.get_plan(session, customer.shop_id, service.plan_id, customer)
            if service.plan_id
            else None
        )
        if (
            plan is None
            or not plan.renewable
            or plan.delivery == "stock"
            or (plan.panel_id and plan.panel_id != service.panel_id)
            or not orders.can_change(service, "renew")
        ):
            out.skipped.append(service)
            continue
        price = (await pricing.plan_quote(session, customer, plan.id, plan.price)).amount
        out.items.append((service, plan, price))
    return out


@dataclass(slots=True)
class RenewResult:
    renewed: list[Service] = field(default_factory=list)
    failed: list[tuple[Service, str]] = field(default_factory=list)


async def renew_many(session: AsyncSession, customer: Customer, services: list[Service]) -> RenewResult:
    """Renew each service with its own plan, paid from the wallet; stops when the money runs out.
    Each renewal is its own order, so one refusal never undoes the others."""
    result = RenewResult()
    planned = await renew_plan(session, customer, services)
    result.failed += [(s, "plan_unavailable") for s in planned.skipped]
    for index, (service, plan, _price) in enumerate(planned.items):
        try:
            async with session.begin_nested():
                draft = await orders.draft(session, customer, OrderKind.RENEW, plan=plan, service=service)
                order = await orders.create(session, customer, draft, source="reseller_bulk")
                await orders.settle_from_wallet(session, order.id)
        except orders.OrderError as exc:
            result.failed.append((service, exc.key))
            if exc.key == "insufficient_balance":
                result.failed += [(s, exc.key) for s, _, _ in planned.items[index + 1 :]]
                break
            continue
        result.renewed.append(service)
    return result


@dataclass(slots=True)
class Report:
    periods: list[tuple[str, int, int]]  # (key, orders, amount)
    top_plans: list[tuple[str, int, int]]  # (title, orders, amount)
    commission: int
    gb_sold: int


async def report(session: AsyncSession, customer: Customer) -> Report:
    now = utcnow()
    bot_shop = await _bot_shop_id(session, customer)
    ord_owner = Order.customer_id == customer.id
    if bot_shop is not None:
        ord_owner = or_(ord_owner, Order.shop_id == bot_shop)
    done = (ord_owner, Order.status == OrderStatus.COMPLETED)
    periods = []
    for key, days in (("today", 1), ("week", 7), ("month", 30)):
        row = await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
                *done, Order.completed_at >= now - timedelta(days=days)
            )
        )
        count, amount = row.one()
        periods.append((key, int(count or 0), int(amount or 0)))
    month = Order.completed_at >= now - timedelta(days=30)
    top = await session.execute(
        select(Plan.title, func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
        .join(Plan, Plan.id == Order.plan_id)
        .where(*done, month)
        .group_by(Plan.title)
        .order_by(func.count(Order.id).desc())
        .limit(5)
    )
    plan_gb = await session.scalar(
        select(func.coalesce(func.sum(Plan.data_gb), 0))
        .select_from(Order)
        .join(Plan, Plan.id == Order.plan_id)
        .where(*done, month, Order.kind.in_([OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE]))
    )
    extra_orders = list(
        await session.scalars(
            select(Order).where(
                *done,
                month,
                or_(Order.kind == OrderKind.EXTRA_VOLUME, Order.plan_id.is_(None)),
            )
        )
    )
    extra_gb = sum(
        int((o.params or {}).get("extra_gb") or (o.params or {}).get("custom_gb") or 0)
        for o in extra_orders
    )
    gb = int(plan_gb or 0) + extra_gb
    commission = await session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0))
        .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(
            LedgerAccount.customer_id == customer.id,
            LedgerJournal.kind == "commission",
            LedgerEntry.created_at >= now - timedelta(days=30),
        )
    )
    return Report(
        periods,
        [(str(title), int(n), int(a)) for title, n, a in top.all()],
        int(commission or 0),
        int(gb or 0),
    )


def remaining_text(service: Service) -> tuple[int | None, float | None]:
    """(days left, GB left) for a list line."""
    days = None
    if service.expire_at is not None:
        days = max(0, (service.expire_at - utcnow()).days)
    left = None
    if service.data_limit:
        left = round(max(0, int(service.data_limit) - int(service.used_bytes)) / GB, 1)
    return days, left


# --- shared by the web panel and the Mini App (TEN-15) ---------------------------------------


def service_row(service: Service, price: int | None) -> dict[str, object]:
    days, left = remaining_text(service)
    return {
        "id": service.id,
        "label": service.label,
        "panel_username": service.panel_username,
        "status": service.status.value,
        "expire_at": service.expire_at.isoformat() if service.expire_at else None,
        "used_bytes": int(service.used_bytes),
        "data_limit": int(service.data_limit),
        "days_left": days,
        "gb_left": left,
        "renew_price": price,
    }


async def expiring_rows(session: AsyncSession, customer: Customer, days: int) -> list[dict[str, object]]:
    """Services about to run out, each with what renewing it with its own plan costs (or None)."""
    items = await expiring(session, customer, days)
    planned = await renew_plan(session, customer, items)
    prices = {service.id: price for service, _plan, price in planned.items}
    return [service_row(s, prices.get(s.id)) for s in items]


async def sell(
    session: AsyncSession, customer: Customer, plan_id: int, label: str | None, *, source: str
) -> Order:
    """One purchase for an end customer, named by `label`, paid from the reseller's wallet."""
    plan = await catalog.get_plan(session, customer.shop_id, plan_id, customer)
    if plan is None:
        raise orders.OrderError("plan_unavailable")
    clean = (label or "").strip()[:48] or None
    draft = await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan, label=clean)
    order = await orders.create(session, customer, draft, source=source)
    await orders.settle_from_wallet(session, order.id)
    return order


async def owned_services(session: AsyncSession, customer: Customer, ids: list[int]) -> list[Service]:
    if not ids:
        return []
    rows = await session.scalars(
        select(Service).where(
            Service.id.in_(ids[:RENEW_LIMIT]),
            Service.customer_id == customer.id,
            Service.status != ServiceStatus.DELETED,
        )
    )
    return list(rows)
