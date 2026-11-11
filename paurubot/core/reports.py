"""The end-of-day summary sent to the reports channel or to staff (FIN-04, JOB-02)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.config import get_settings
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerAccountKind,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    PaymentStatus,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
    TrialGrant,
)


@dataclass(slots=True)
class DailySummary:
    orders: int
    revenue: int
    renewals: int
    new_customers: int
    trials: int
    active_services: int
    expiring_soon: int
    pending_receipts: int
    open_tickets: int
    panels_down: int
    wallet_total: int


def report_day(now: datetime, hour: int) -> datetime:
    """A moment in the day the latest scheduled daily report covers (REF-06).

    The report is due from `hour` on, not only during it: a worker that was down or busy for that
    hour still sends it later. One sent in the small hours (before 06:00) covers the day that had
    just ended, as before.
    """
    hour = max(0, min(23, int(hour)))
    local = now.astimezone(get_settings().tz)
    due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if due > local:
        due -= timedelta(days=1)
    return due - timedelta(hours=6) if hour < 6 else due


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    """Midnight to midnight in the shop's time zone, as aware datetimes."""
    tz = get_settings().tz
    local = day.astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def real_buyer() -> Any:
    """Orders of customers staff did not mark as a test account (ClockBot's demo customers)."""
    return Order.customer_id.not_in(select(Customer.id).where(Customer.is_test.is_(True)))


async def daily_summary(session: AsyncSession, shop_id: int, start: datetime, end: datetime) -> DailySummary:
    async def count(stmt) -> int:  # type: ignore[no-untyped-def]
        return int(await session.scalar(stmt) or 0)

    sold = (
        Order.shop_id == shop_id,
        Order.status == OrderStatus.COMPLETED,
        Order.completed_at >= start,
        Order.completed_at < end,
        real_buyer(),
    )
    return DailySummary(
        orders=await count(select(func.count(Order.id)).where(*sold)),
        revenue=await count(select(func.coalesce(func.sum(Order.amount), 0)).where(*sold)),
        renewals=await count(select(func.count(Order.id)).where(*sold, Order.kind == OrderKind.RENEW)),
        new_customers=await count(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop_id,
                Customer.telegram_id > 0,  # unclaimed web guests are not people who started the bot
                Customer.created_at >= start,
                Customer.created_at < end,
            )
        ),
        trials=await count(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.shop_id == shop_id, TrialGrant.created_at >= start, TrialGrant.created_at < end
            )
        ),
        active_services=await count(
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
            )
        ),
        expiring_soon=await count(
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id,
                Service.status == ServiceStatus.ACTIVE,
                Service.expire_at >= end,
                Service.expire_at < end + timedelta(days=3),
            )
        ),
        pending_receipts=await count(
            select(func.count(Payment.id)).where(
                Payment.shop_id == shop_id, Payment.status == PaymentStatus.VERIFYING
            )
        ),
        open_tickets=await count(
            select(func.count(Ticket.id)).where(Ticket.shop_id == shop_id, Ticket.status == TicketStatus.OPEN)
        ),
        panels_down=await count(
            select(func.count(Panel.id)).where(
                Panel.shop_id == shop_id, Panel.is_active.is_(True), Panel.is_healthy.is_(False)
            )
        ),
        wallet_total=await count(
            select(func.coalesce(func.sum(LedgerAccount.balance), 0)).where(
                LedgerAccount.shop_id == shop_id, LedgerAccount.kind == LedgerAccountKind.WALLET
            )
        ),
    )


def as_params(summary: DailySummary) -> dict[str, int]:
    return asdict(summary)


# --- the nightly report's details: sales by kind and hour, users, panels ---------------------

NEW_KINDS = {OrderKind.PURCHASE, OrderKind.ACCOUNT_PURCHASE}
RENEW_KINDS = {OrderKind.RENEW, OrderKind.ACCOUNT_RENEW}
GB = 1024**3


@dataclass(slots=True)
class PanelDay:
    name: str
    orders: int = 0
    amount: int = 0
    gb: int = 0


@dataclass(slots=True)
class DailyDetails:
    purchases: int
    purchases_amount: int
    renewals: int
    renewals_amount: int
    others_amount: int  # extras, upgrades, moves and admin-account add-ons
    total: int
    yesterday_total: int
    best_hour: int | None
    active_users: int
    inactive_users: int
    panels: list[PanelDay]

    @property
    def change_percent(self) -> int | None:
        """Today's sales against yesterday's, rounded; None when yesterday sold nothing."""
        if not self.yesterday_total:
            return None
        return round((self.total - self.yesterday_total) * 100 / self.yesterday_total)


def _sold_gb(order: Order) -> int:
    params = order.params or {}
    custom = params.get("custom") if isinstance(params.get("custom"), dict) else None
    if custom:
        return int(custom.get("gb") or 0)
    if order.kind == OrderKind.EXTRA_VOLUME:
        return int(params.get("extra_gb") or 0)
    if order.plan is not None and order.kind in {OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE}:
        return int(order.plan.data_gb or 0)
    return 0


async def daily_details(session: AsyncSession, shop_id: int, start: datetime, end: datetime) -> DailyDetails:
    """What the nightly report shows beside the summary: the day's sales by kind and by panel,
    the busiest hour, the change against the day before, and active against inactive customers."""
    rows = list(
        await session.scalars(
            select(Order).where(
                Order.shop_id == shop_id,
                Order.status == OrderStatus.COMPLETED,
                Order.completed_at >= start,
                Order.completed_at < end,
                Order.kind != OrderKind.TRIAL,
                real_buyer(),
            )
        )
    )
    yesterday = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(
                Order.shop_id == shop_id,
                Order.status == OrderStatus.COMPLETED,
                Order.completed_at >= start - timedelta(days=1),
                Order.completed_at < start,
                Order.kind != OrderKind.TRIAL,
                real_buyer(),
            )
        )
        or 0
    )
    tz = get_settings().tz
    hours: dict[int, int] = {}
    for order in rows:
        if order.completed_at is not None:
            hour = order.completed_at.astimezone(tz).hour
            hours[hour] = hours.get(hour, 0) + 1
    service_panels = dict(
        (
            await session.execute(
                select(Service.id, Service.panel_id).where(
                    Service.id.in_({o.service_id for o in rows if o.service_id})
                )
            )
        ).all()
    )
    all_panels = list(await session.scalars(select(Panel).where(Panel.shop_id == shop_id).order_by(Panel.id)))
    active_ids = {panel.id for panel in all_panels if panel.is_active}
    panels = {panel.id: PanelDay(panel.name) for panel in all_panels}
    for order in rows:
        panel_id = service_panels.get(order.service_id) if order.service_id else None
        if panel_id in panels:
            day = panels[panel_id]
            day.orders += 1
            day.amount += int(order.amount)
            day.gb += _sold_gb(order)
    people = int(
        await session.scalar(
            select(func.count(Customer.id)).where(Customer.shop_id == shop_id, Customer.telegram_id > 0)
        )
        or 0
    )
    active = int(
        await session.scalar(
            select(func.count(func.distinct(Service.customer_id))).where(
                Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
            )
        )
        or 0
    )
    new = [o for o in rows if o.kind in NEW_KINDS]
    renew = [o for o in rows if o.kind in RENEW_KINDS]
    return DailyDetails(
        purchases=len(new),
        purchases_amount=sum(int(o.amount) for o in new),
        renewals=len(renew),
        renewals_amount=sum(int(o.amount) for o in renew),
        others_amount=sum(int(o.amount) for o in rows if o.kind not in NEW_KINDS | RENEW_KINDS),
        total=sum(int(o.amount) for o in rows),
        yesterday_total=yesterday,
        best_hour=max(hours, key=lambda h: (hours[h], -h)) if hours else None,
        active_users=active,
        inactive_users=max(0, people - active),
        # A switched-off panel only shows when it still sold something that day.
        panels=[day for pid, day in panels.items() if pid in active_ids or day.orders],
    )


# --- sales funnel (ADM-06) ----------------------------------------------------------------

FUNNEL_STEPS = ("joined", "phone", "trial", "first_purchase", "repeat")


async def funnel(
    session: AsyncSession, shop_id: int, start: datetime, end: datetime, customer_ids: list[int] | None = None
) -> dict[str, Any]:
    """Of the people who started the bot in the period: how many reached each step, ever."""

    cohort = select(Customer.id).where(
        Customer.shop_id == shop_id,
        Customer.telegram_id > 0,
        Customer.created_at >= start,
        Customer.created_at < end,
    )
    if customer_ids is not None:
        cohort = cohort.where(Customer.id.in_(customer_ids))
    ids = list(await session.scalars(cohort))
    if not ids:
        return {"steps": [{"key": k, "count": 0} for k in FUNNEL_STEPS], "days_to_first": None}
    paid = (
        select(
            Order.customer_id, func.count(Order.id).label("n"), func.min(Order.completed_at).label("first")
        )
        .where(
            Order.customer_id.in_(ids),
            Order.status == OrderStatus.COMPLETED,
            Order.amount > 0,
            Order.kind != OrderKind.TRIAL,
        )
        .group_by(Order.customer_id)
        .subquery()
    )
    buyers = (await session.execute(select(paid.c.customer_id, paid.c.n, paid.c.first))).all()
    joined_at = dict(
        (await session.execute(select(Customer.id, Customer.created_at).where(Customer.id.in_(ids)))).all()
    )
    phone = int(
        await session.scalar(
            select(func.count(Customer.id)).where(Customer.id.in_(ids), Customer.phone.is_not(None))
        )
        or 0
    )
    trial = int(
        await session.scalar(
            select(func.count(func.distinct(TrialGrant.customer_id))).where(
                TrialGrant.customer_id.in_(ids), TrialGrant.status == "issued"
            )
        )
        or 0
    )
    waits = sorted(
        (first - joined_at[cid]).total_seconds() / 86400
        for cid, _n, first in buyers
        if first and cid in joined_at
    )
    median = round(waits[len(waits) // 2], 1) if waits else None
    counts = {
        "joined": len(ids),
        "phone": phone,
        "trial": trial,
        "first_purchase": len(buyers),
        "repeat": sum(1 for _cid, n, _first in buyers if n >= 2),
    }
    return {"steps": [{"key": k, "count": counts[k]} for k in FUNNEL_STEPS], "days_to_first": median}


@dataclass(slots=True)
class ResellerDay:
    orders: int
    spent: int
    balance: int
    bot_orders: int
    bot_revenue: int


async def reseller_day(session: AsyncSession, profile: Any, start: datetime, end: datetime) -> ResellerDay:
    """A reseller's own day (ClockBot's reseller daily report): what it bought here, its wallet,
    and what its own bot sold, if it has one."""
    from paurubot.db.models import Order, OrderStatus, Shop
    from paurubot.ledger.accounts import wallet_balance

    bought = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
                Order.customer_id == profile.customer_id,
                Order.status == OrderStatus.COMPLETED,
                Order.completed_at >= start,
                Order.completed_at < end,
            )
        )
    ).one()
    balance = await wallet_balance(session, profile.shop_id, profile.customer_id)
    bot = await session.scalar(select(Shop).where(Shop.reseller_profile_id == profile.id))
    bot_orders, bot_revenue = 0, 0
    if bot is not None:
        sold = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
                    Order.shop_id == bot.id,
                    Order.status == OrderStatus.COMPLETED,
                    Order.completed_at >= start,
                    Order.completed_at < end,
                )
            )
        ).one()
        bot_orders, bot_revenue = int(sold[0] or 0), int(sold[1] or 0)
    return ResellerDay(int(bought[0] or 0), int(bought[1] or 0), int(balance), bot_orders, bot_revenue)
