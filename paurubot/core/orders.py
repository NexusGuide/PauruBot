"""Order state machine (purchase, renew, extras, upgrade).

pending ──pay──▶ paid ──worker──▶ provisioning ──▶ completed
   │                                   └─fail(final)─▶ failed ─refund─▶ refunded
   └──timeout/cancel──▶ canceled
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
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
    LedgerAccount,
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
from paurubot.ledger.accounts import system_account, wallet_account, wallet_balance
from paurubot.ledger.posting import InsufficientFunds
from paurubot.worker import queue

PROVISION_QUEUE = "provision"
ACCOUNT_KINDS = frozenset(
    {
        OrderKind.ACCOUNT_PURCHASE,
        OrderKind.ACCOUNT_RENEW,
        OrderKind.ACCOUNT_GB,
        OrderKind.ACCOUNT_USERS,
        OrderKind.ACCOUNT_DAYS,
    }
)


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


# Order kinds that start a service period with a price paid for it (SEC-F01).
PERIOD_KINDS = (OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE)


@dataclass(slots=True, frozen=True)
class PeriodBasis:
    """What was really paid for a service's current period (SEC-F01)."""

    paid: int  # by the customer, including credit an upgrade carried over
    wholesale: int  # by the reseller, for a reseller-bot sale, likewise
    days: int  # the period's plan days
    share: float  # unused part of the period: the smaller of time and data left (0..1)

    @property
    def credit(self) -> int:
        return int(self.paid * self.share)

    @property
    def wholesale_credit(self) -> int:
        return int(self.wholesale * self.share)


async def period_basis(session: AsyncSession, service: Service) -> PeriodBasis:
    """The latest completed purchase, renewal or upgrade, and how much of it is unused.

    Time alone would credit a service whose data is used up almost in full, and the
    upgrade then resets the data. List price would credit discounts and later price
    rises. The share is the one the self-delete refund uses. Free, gifted, staff-made
    and trial periods are worth 0.
    """
    from paurubot.core.cancellation import unused_share

    last = await session.scalar(
        select(Order)
        .where(
            Order.service_id == service.id,
            Order.status == OrderStatus.COMPLETED,
            Order.kind.in_(PERIOD_KINDS),
        )
        .order_by(Order.completed_at.desc().nulls_last(), Order.id.desc())
        .limit(1)
    )
    if last is None or service.is_trial:
        return PeriodBasis(0, 0, 0, 0.0)
    params = last.params or {}
    paid, wholesale = int(last.amount or 0), int(params.get("wholesale") or 0)
    if last.kind == OrderKind.UPGRADE:
        paid += max(0, int(params.get("credit") or 0))
        if wholesale:
            wholesale += max(0, int(params.get("wholesale_credit") or 0))
    plan = await session.get(Plan, last.plan_id) if last.plan_id else service.plan
    days = int(plan.days) if plan is not None else 0
    return PeriodBasis(paid, wholesale, days, unused_share(service, days))


async def upgrade_credit(session: AsyncSession, service: Service) -> int:
    """Unused value of the current period, as paid: paid amount × min(time, data) left."""
    return (await period_basis(session, service)).credit


async def upgrade_floor(session: AsyncSession, service: Service) -> int:
    """An upgrade must cost more than this: the current plan's price, else the period's value."""
    if service.plan is not None:
        return int(service.plan.price)
    return (await period_basis(session, service)).paid


def upgradable(plan: Plan, service: Service, floor: int) -> bool:
    return plan.id != service.plan_id and int(plan.price) > floor


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


EXTRA_CHOICES = {"gb": (5, 10, 20, 50, 100), "day": (7, 15, 30, 60, 90)}
EXTRA_CAP = {"gb": 10_000, "day": 3650}


def extra_bounds(cfg: dict[str, Any], unit: str) -> tuple[int, int]:
    """The least and most extra GB (`unit` "gb") or days ("day") one order may buy."""
    cap = EXTRA_CAP[unit]
    low = min(max(1, int(cfg.get(f"extra_{unit}_min") or 0)), cap)
    high = min(int(cfg.get(f"extra_{unit}_max") or 0) or cap, cap)
    return low, max(low, high)


def extra_choices(cfg: dict[str, Any], unit: str) -> list[int]:
    """The amounts a customer is offered: the usual steps inside the shop's bounds, plus the
    bounds the shop set itself, so a range between two steps still has something to pick."""
    low, high = extra_bounds(cfg, unit)
    picks = {value for value in EXTRA_CHOICES[unit] if low <= value <= high}
    if int(cfg.get(f"extra_{unit}_min") or 0) > 0:
        picks.add(low)
    if int(cfg.get(f"extra_{unit}_max") or 0) > 0:
        picks.add(high)
    return sorted(picks) or [low]


def can_change(service: Service, change: str) -> bool:
    """Whether the service's panel can apply `change` (renew, upgrade, extra_volume...) at all."""
    from paurubot.panels.registry import changes_of

    if service.panel is not None and service.panel.national_mode:
        return change == "renew"
    return change in changes_of(service.panel.kind if service.panel is not None else None)


def service_extras(cfg: dict[str, Any], service: Service) -> tuple[int | None, int | None]:
    """Unit prices of extra data and extra days for one service; None where it isn't sold.

    The plan decides prices (CAT-02); a panel can switch either one off (ADM-07), and some panel
    kinds cannot take extras at all (PNL-11).
    """
    if service.is_trial or (service.panel is not None and service.panel.national_mode):
        return None, None
    prices = extra_prices(cfg, service.plan)
    if prices is None:
        return None, None
    options = (service.panel.options if service.panel is not None else None) or {}
    gb = (
        None
        if options.get("sell_extra_volume") is False or not can_change(service, "extra_volume")
        else prices[0]
    )
    days = (
        None
        if options.get("sell_extra_days") is False or not can_change(service, "extra_days")
        else prices[1]
    )
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
    wanted_username: str | None = None,
    package: int | None = None,
) -> Draft:
    from paurubot.core import own_panel, resale, usernames

    # A dedicated own-panel bot sells only while its admin account can serve (OPB).
    blocked = await own_panel.sales_gate(session, customer.shop_id, new_user=kind == OrderKind.PURCHASE)
    if blocked:
        raise OrderError(blocked)
    shop_resale = await resale.for_shop(session, customer.shop_id)
    if shop_resale is not None:
        # A reseller's own bot sells the parent's plans: purchases and renewals always (TEN-04),
        # extras and upgrades only when staff allow them and the reseller turned them on (TEN-18).
        if not shop_resale.open:
            raise OrderError("shop_unavailable")
        if kind in {OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS}:
            if not shop_resale.offers("extras"):
                raise OrderError("extras_unavailable")
        elif kind == OrderKind.UPGRADE:
            if not shop_resale.offers("upgrade"):
                raise OrderError("plan_unavailable")
            if plan is None or plan.shop_id != shop_resale.parent_id:
                raise OrderError("plan_unavailable")
        elif (
            kind not in {OrderKind.PURCHASE, OrderKind.RENEW}
            or plan is None
            or plan.shop_id != shop_resale.parent_id
        ):
            raise OrderError("plan_unavailable")
        try:
            # The reseller's own limits cover what its bot sells, too.
            await resellers.check_purchase(
                session,
                shop_resale.reseller,
                kind=kind,
                add_gb=(
                    plan.data_gb
                    if kind == OrderKind.PURCHASE and plan is not None
                    else (extra_gb if kind == OrderKind.EXTRA_VOLUME else 0)
                ),
                bot_shop_id=shop_resale.shop.id,
            )
        except resellers.ResellerError as exc:
            raise OrderError("shop_unavailable") from exc
    d = await _draft(
        session,
        customer,
        kind,
        plan=plan,
        service=service,
        extra_gb=extra_gb,
        extra_days=extra_days,
        label=label,
        custom_gb=custom_gb,
        custom_days=custom_days,
        move_to=move_to,
        # Extras and upgrades in a reseller bot follow the parent shop's own rules and prices.
        settings_shop_id=shop_resale.parent_id if shop_resale is not None else None,
        # Ready-made packages are the shop's own prices: not in a reseller bot, priced per unit.
        package=package if shop_resale is None else None,
    )
    name = usernames.wanted(wanted_username) if kind == OrderKind.PURCHASE else None
    if name:
        d.params["wanted_username"] = name  # the customer's own service name (CAT-12)
    if shop_resale is not None:
        if kind in {OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS}:
            assert service is not None
            gb_cost, day_cost = await resale.extra_units(session, shop_resale, service)
            unit = gb_cost if kind == OrderKind.EXTRA_VOLUME else day_cost
            if unit is None:
                raise OrderError("extras_unavailable")
            count = extra_gb if kind == OrderKind.EXTRA_VOLUME else extra_days
            cost, listed = unit * count, resale.retail(shop_resale, None, unit) * count
        elif kind == OrderKind.UPGRADE:
            assert service is not None and plan is not None
            cost, listed, wholesale_credit = await resale.upgrade_prices(session, shop_resale, service, plan)
            d.params["wholesale_credit"] = wholesale_credit
        else:
            cost = await resale.wholesale(session, shop_resale, plan.id if plan else None, d.base_price)
            listed = resale.retail(shop_resale, plan.id if plan else None, cost)
        if cost <= 0:
            raise OrderError("plan_unavailable")  # nothing for the reseller to pay means nothing to resell
        d.params.update(wholesale=cost, resale_profile=shop_resale.profile.id)
        d.base_price = listed
    return d


async def _draft(
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
    settings_shop_id: int | None = None,
    package: int | None = None,
) -> Draft:
    cfg = await shop_settings.load(session, settings_shop_id or customer.shop_id)
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
        if kind in {OrderKind.RENEW, OrderKind.UPGRADE} and not can_change(service, kind.value):
            raise OrderError("plan_unavailable")
    from paurubot.core import national, stock

    if (
        service is not None
        and service.panel is not None
        and service.panel.national_mode
        and kind != OrderKind.RENEW
    ):
        raise OrderError("plan_unavailable")
    if plan is not None and kind in {OrderKind.PURCHASE, OrderKind.RENEW}:
        try:
            target = await national.route(session, plan, service)
        except stock.StockError as exc:
            raise OrderError(str(exc)) from exc
        if target is not None:
            if (await stock.counts(session, [target.id]))[target.id][stock.AVAILABLE] < 1:
                raise OrderError("out_of_stock")
            params["stock_plan_id"] = target.id
            params["national"] = True
    if kind == OrderKind.PURCHASE:
        if plan is None and (custom_gb or custom_days):
            params["custom"] = {"gb": custom_gb, "days": custom_days}
            return Draft(kind, None, None, params, custom_price(cfg, custom_gb, custom_days))
        if plan is None:
            raise OrderError("plan_unavailable")
        if plan.delivery == "stock":
            from paurubot.core import stock

            if (await stock.counts(session, [plan.id]))[plan.id][stock.AVAILABLE] < 1:
                raise OrderError("out_of_stock")
        return Draft(kind, plan, None, params, plan.price)
    if kind == OrderKind.RENEW:
        assert service is not None
        if service.panel is not None and (service.panel.options or {}).get("allow_renewals") is False:
            raise OrderError("plan_unavailable")
        if plan is None or not plan.renewable or plan.delivery == "stock":
            raise OrderError("plan_unavailable")
        if plan.panel_id and plan.panel_id != service.panel_id:
            raise OrderError("plan_wrong_panel")
        return Draft(kind, plan, service, params, plan.price)
    if kind in {OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS} and package is not None:
        # A ready-made package (ClockBot parity): its own amount and price, when extras are on.
        assert service is not None
        unit = "gb" if kind == OrderKind.EXTRA_VOLUME else "days"
        packs = cfg.get("extra_packages") or []
        pack = packs[package] if 0 <= package < len(packs) else None
        amount = extra_gb if unit == "gb" else extra_days
        gb_price, day_price = service_extras(cfg, service)
        if (
            pack is None
            or pack.get("unit") != unit
            or int(pack.get("amount") or 0) != amount
            or (gb_price if unit == "gb" else day_price) is None
        ):
            raise OrderError("extras_unavailable")
        params["extra_gb" if unit == "gb" else "extra_days"] = amount
        params["package"] = package
        return Draft(kind, None, service, params, int(pack["price"]))
    if kind == OrderKind.EXTRA_VOLUME:
        assert service is not None
        gb_price, _ = service_extras(cfg, service)
        low, high = extra_bounds(cfg, "gb")
        if gb_price is None or not low <= extra_gb <= high:
            raise OrderError("extras_unavailable")
        params["extra_gb"] = extra_gb
        return Draft(kind, None, service, params, extra_gb * gb_price)
    if kind == OrderKind.EXTRA_DAYS:
        assert service is not None
        _, day_price = service_extras(cfg, service)
        low, high = extra_bounds(cfg, "day")
        if day_price is None or not low <= extra_days <= high:
            raise OrderError("extras_unavailable")
        params["extra_days"] = extra_days
        return Draft(kind, None, service, params, extra_days * day_price)
    if kind == OrderKind.UPGRADE:
        assert service is not None
        if not cfg["upgrade_enabled"] or plan is None or plan.delivery == "stock":
            raise OrderError("plan_unavailable")
        if plan.panel_id and plan.panel_id != service.panel_id:
            raise OrderError("plan_wrong_panel")
        if not upgradable(plan, service, await upgrade_floor(session, service)):
            raise OrderError("upgrade_not_higher")
        credit = await upgrade_credit(session, service)
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


# Orders that will become a service: paid and on their way, or being paid (a receipt or a
# gateway payment under way). A merely opened checkout does not count, so pressing «buy» twice
# never locks a customer out.
IN_FLIGHT = [OrderStatus.PAID, OrderStatus.PROVISIONING]
PAYING = [OrderStatus.AWAITING_PAYMENT]
# Where the customer can see and accept the terms: the bot and the Mini App (guest web checkout
# and staff desks never asked for them).
TERMS_SOURCES = {"bot", "webapp", "bulk"}


async def check_purchase_terms(session: AsyncSession, customer: Customer) -> None:
    """With the phone or the terms asked «before purchase», a new purchase waits for them."""
    cfg = await shop_settings.load(session, customer.shop_id)
    if cfg.get("require_phone") and cfg.get("phone_when") == "purchase" and not customer.phone:
        raise OrderError("phone_required")
    if customer.is_reseller:
        # Resellers accept their own purchase terms, when the shop has them, instead.
        if cfg.get("reseller_terms_required") and customer.reseller_terms_version != int(
            cfg.get("reseller_terms_version") or 1
        ):
            raise OrderError("reseller_terms_required")
        return
    if (
        cfg.get("terms_required")
        and cfg.get("terms_when") == "purchase"
        and customer.terms_accepted_version != int(cfg.get("terms_version") or 1)
    ):
        raise OrderError("terms_required")


async def check_purchase_limits(session: AsyncSession, customer: Customer) -> None:
    """The shop's caps on new services for one customer (settings › limits).

    Orders being paid count until they expire, beside live services and paid orders."""
    if customer.is_reseller:
        return  # resellers have their own caps on their profile (TEN-05)
    cfg = await shop_settings.load(session, customer.shop_id)
    live_cap = max(0, int(cfg.get("max_active_services") or 0))
    daily_cap = max(0, int(cfg.get("max_daily_purchases") or 0))
    if not live_cap and not daily_cap:
        return
    now = utcnow()
    counting = (Order.status.in_(IN_FLIGHT)) | (
        Order.status.in_(PAYING) & ((Order.expires_at.is_(None)) | (Order.expires_at > now))
    )
    purchases = select(func.count(Order.id)).where(
        Order.customer_id == customer.id, Order.kind == OrderKind.PURCHASE
    )
    if live_cap:
        live = await session.scalar(
            select(func.count(Service.id)).where(
                Service.customer_id == customer.id, Service.status.in_(resellers.LIVE)
            )
        )
        coming = await session.scalar(purchases.where(counting))
        if int(live or 0) + int(coming or 0) >= live_cap:
            raise OrderError("limit_active_services", limit=live_cap)
    if daily_cap:
        today = await session.scalar(
            purchases.where(
                Order.created_at >= now - timedelta(days=1),
                counting | (Order.status == OrderStatus.COMPLETED),
            )
        )
        if int(today or 0) >= daily_cap:
            raise OrderError("limit_daily_purchases", limit=daily_cap)


async def create(
    session: AsyncSession,
    customer: Customer,
    d: Draft,
    *,
    code: DiscountCode | None = None,
    source: str = "bot",
) -> Order:
    if d.kind == OrderKind.PURCHASE:
        if source in TERMS_SOURCES:
            await check_purchase_terms(session, customer)
        await check_purchase_limits(session, customer)
    if resellers.metered(customer) and d.kind not in ACCOUNT_KINDS:
        code = None  # nothing to discount: the order itself is free
    if code is not None:
        await pricing.validate_code(
            session, code, customer, plan_id=d.plan.id if d.plan else None, amount=d.base_price
        )
    account = d.kind in ACCOUNT_KINDS
    # Panel admin accounts carry no reseller discount unless their plan opts in (TEN-14).
    no_reseller_price = account and not d.params.get("reseller_priced")
    if d.plan is not None and d.kind in {OrderKind.PURCHASE, OrderKind.RENEW}:
        q = await pricing.plan_quote(session, customer, d.plan.id, d.base_price, code)  # TEN-12
    else:
        q = pricing.quote(d.base_price, None if no_reseller_price else customer, code)
    if code is None and resellers.metered(customer) and not account:
        # Metered resellers pay for usage or service-hours instead of per order (TEN-05).
        q = pricing.Quote(d.base_price, d.base_price, 0, 0)
        d.params = {**d.params, "metered": True}
    if q.reseller_discount and not customer.is_reseller:
        d.params = {**d.params, "tier": pricing.reseller_percent(customer)}  # a loyalty discount (GRW-02)
    # A reseller's discount code may reduce only its own margin, never the wholesale floor.
    wholesale = int(d.params.get("wholesale") or 0)
    if wholesale > 0 and q.amount < wholesale:
        after_reseller = max(0, q.base_price - q.reseller_discount)
        q = pricing.Quote(
            q.base_price,
            q.reseller_discount,
            max(0, after_reseller - wholesale),
            wholesale,
            q.code,
        )
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


async def _lock_customer_settlement(session: AsyncSession, order: Order) -> None:
    """Serialize this customer's settlements before checking per-customer discount rules (SEC-F02).

    Locks the very rows every ledger posting locks, in the same (id) order: the customer's wallet
    and the shop's revenue account. That keeps one lock order everywhere (no new deadlock), and
    still serializes orders that charge nothing, such as a 100% code.
    """
    accounts = {
        (await wallet_account(session, order.shop_id, order.customer_id)).id,
        (await system_account(session, order.shop_id, "revenue")).id,
    }
    await session.execute(
        select(LedgerAccount.id)
        .where(LedgerAccount.id.in_(sorted(accounts)))
        .order_by(LedgerAccount.id)
        .with_for_update()
    )


async def _check_customer_rules(session: AsyncSession, order: Order, code: DiscountCode) -> None:
    """Per-customer and first-purchase limits, decided under the settlement lock.

    The quote itself is honoured: a code that expired or was switched off after the order was
    created still applies until the order expires. Only limits that concurrent or earlier unpaid
    orders could otherwise all pass at creation are checked again here.
    """
    if not code.per_customer_limit and not code.first_purchase_only:
        return
    await _lock_customer_settlement(session, order)
    if code.per_customer_limit:
        used = await session.scalar(
            select(func.count(DiscountRedemption.id)).where(
                DiscountRedemption.code_id == code.id, DiscountRedemption.customer_id == order.customer_id
            )
        )
        if int(used or 0) >= code.per_customer_limit:
            raise OrderError("discount_already_used")
    if code.first_purchase_only and await pricing.has_prior_purchase(
        session, order.customer_id, exclude_order_id=order.id
    ):
        raise OrderError("discount_first_purchase_only")


async def _redeem(session: AsyncSession, order: Order) -> None:
    if not order.discount_code_id:
        return
    rules = await session.get(DiscountCode, order.discount_code_id)
    if rules is not None:
        await _check_customer_rules(session, order, rules)
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
        # One savepoint: a discount code that just ran out must not leave the wallet charged.
        async with session.begin_nested():
            await wallet.charge(session, order.shop_id, order.customer_id, order.amount, order_id=order.id)
            await _redeem(session, order)
            from paurubot.core import national, stock

            try:
                await national.validate_checkout(session, order)
            except stock.StockError as exc:
                raise OrderError(str(exc)) from exc
            if order.params.get("stock_plan_id") and await stock.take(session, order, reserve=True) is None:
                raise OrderError("out_of_stock")
    except InsufficientFunds as exc:
        balance = await wallet_balance(session, order.shop_id, order.customer_id)
        raise OrderError("insufficient_balance", balance=balance, required=order.amount) from exc
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await enqueue_provision(session, order)
    await session.flush()
    return order


async def queued_until(session: AsyncSession, order: Order) -> datetime | None:
    """When a renewal should wait for the current period to end (SVC-02), the time it runs."""
    if order.params.get("stock_plan_id") or order.kind != OrderKind.RENEW or not order.service_id:
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
    # Pending and canceled orders were never charged, so there is nothing to give back.
    if order.status in {
        OrderStatus.REFUNDED,
        OrderStatus.COMPLETED,
        OrderStatus.PENDING,
        OrderStatus.CANCELED,
    }:
        return
    await wallet.refund(
        session, order.shop_id, order.customer_id, order.amount, order_id=order.id, actor=actor
    )
    order.status = OrderStatus.REFUNDED
    order.error = reason[:500]
    await release_holds(session, order)


async def release_holds(session: AsyncSession, order: Order) -> None:
    """Give back what a refunded order held: a reserved stock item and its discount code use."""
    from paurubot.core import stock
    from paurubot.db.models import StockItem

    await session.execute(
        update(StockItem)
        .where(StockItem.order_id == order.id, StockItem.status == stock.RESERVED)
        .values(status=stock.AVAILABLE, order_id=None, customer_id=None, sold_at=None)
    )
    await session.execute(delete(DiscountRedemption).where(DiscountRedemption.order_id == order.id))
    if order.discount_code_id:
        await session.execute(
            update(DiscountCode)
            .where(DiscountCode.id == order.discount_code_id, DiscountCode.used_count > 0)
            .values(used_count=DiscountCode.used_count - 1)
        )
