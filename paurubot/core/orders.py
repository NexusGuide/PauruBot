"""Order state machine (purchase, renew, extras, upgrade).

pending ──pay──▶ paid ──worker──▶ provisioning ──▶ completed
   │                                   └─fail(final)─▶ failed ─refund─▶ refunded
   └──timeout/cancel──▶ canceled
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
ACCOUNT_KINDS = frozenset(
    {OrderKind.ACCOUNT_PURCHASE, OrderKind.ACCOUNT_RENEW, OrderKind.ACCOUNT_GB, OrderKind.ACCOUNT_USERS}
)
NOTE_MAX = 150
# An owned one-buy plan blocks a new purchase once it reached one of these states.
OWNED_STATUSES = (OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED)


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


def truncate_note(note: str | None) -> str | None:
    """Purchase-time note kept on the order, at most 150 characters."""
    if note is None:
        return None
    text = str(note).strip()
    return text[:NOTE_MAX] if text else None


async def _ensure_one_buy(session: AsyncSession, customer: Customer, plan: Plan) -> None:
    """Refuse a second purchase of a one-buy-only plan (productsoldout-style guard)."""
    if not getattr(plan, "one_buy", False):
        return
    owned = await session.scalar(
        select(Order.id)
        .where(
            Order.customer_id == customer.id,
            Order.plan_id == plan.id,
            Order.status.in_(OWNED_STATUSES),
        )
        .limit(1)
    )
    if owned is not None:
        raise OrderError("one_buy_already_owned", plan_id=plan.id)


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


def can_change(service: Service, change: str) -> bool:
    """Whether the service's panel can apply `change` (renew, upgrade, extra_volume...) at all."""
    from paurubot.panels.registry import changes_of

    return change in changes_of(service.panel.kind if service.panel is not None else None)


def service_extras(cfg: dict[str, Any], service: Service) -> tuple[int | None, int | None]:
    """Unit prices of extra data and extra days for one service; None where it isn't sold.

    The plan decides prices (CAT-02); a panel can switch either one off (ADM-07), and some panel
    kinds cannot take extras at all (PNL-11).
    """
    if service.is_trial:
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
    note: str | None = None,
) -> Draft:
    from paurubot.core import resale, usernames

    shop_resale = await resale.for_shop(session, customer.shop_id)
    if shop_resale is not None:
        # A reseller's own bot sells the parent's plans, purchases and renewals only (TEN-04).
        if not shop_resale.open:
            raise OrderError("shop_unavailable")
        if (
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
                add_gb=plan.data_gb if kind == OrderKind.PURCHASE else 0,
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
        note=note,
    )
    name = usernames.wanted(wanted_username) if kind == OrderKind.PURCHASE else None
    if name:
        d.params["wanted_username"] = name  # the customer's own service name (CAT-12)
    if shop_resale is not None:
        cost = await resale.wholesale(session, shop_resale, plan.id if plan else None, d.base_price)
        if cost <= 0:
            raise OrderError("plan_unavailable")  # nothing for the reseller to pay means nothing to resell
        d.params.update(wholesale=cost, resale_profile=shop_resale.profile.id)
        d.base_price = resale.retail(shop_resale, plan.id if plan else None, cost)
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
    note: str | None = None,
) -> Draft:
    cfg = await shop_settings.load(session, customer.shop_id)
    params: dict[str, Any] = {}
    if label:
        params["label"] = label[:48]
    trimmed = truncate_note(note)
    if trimmed:
        params["customer_note"] = trimmed
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
    if kind == OrderKind.PURCHASE:
        if plan is None and (custom_gb or custom_days):
            params["custom"] = {"gb": custom_gb, "days": custom_days}
            return Draft(kind, None, None, params, custom_price(cfg, custom_gb, custom_days))
        if plan is None:
            raise OrderError("plan_unavailable")
        await _ensure_one_buy(session, customer, plan)
        if plan.delivery == "stock":
            from paurubot.core import stock

            if (await stock.counts(session, [plan.id]))[plan.id][stock.AVAILABLE] < 1:
                raise OrderError("out_of_stock")
        return Draft(kind, plan, None, params, plan.price)
    if kind == OrderKind.RENEW:
        assert service is not None
        if plan is None or not plan.renewable or plan.delivery == "stock":
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
        if not cfg["upgrade_enabled"] or plan is None or plan.delivery == "stock":
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
    note: str | None = None,
) -> Order:
    trimmed = truncate_note(note)
    if trimmed:
        d.params = {**d.params, "customer_note": trimmed}
    if d.kind == OrderKind.PURCHASE and d.plan is not None:
        await _ensure_one_buy(session, customer, d.plan)
    if resellers.metered(customer) and d.kind not in ACCOUNT_KINDS:
        code = None  # nothing to discount: the order itself is free
    if code is not None:
        await pricing.validate_code(
            session, code, customer, plan_id=d.plan.id if d.plan else None, amount=d.base_price
        )
    account = d.kind in ACCOUNT_KINDS  # panel admin accounts carry no reseller discount (TEN-14)
    if d.plan is not None and d.kind in {OrderKind.PURCHASE, OrderKind.RENEW}:
        q = await pricing.plan_quote(session, customer, d.plan.id, d.base_price, code)  # TEN-12
    else:
        q = pricing.quote(d.base_price, None if account else customer, code)
    if code is None and resellers.metered(customer) and not account:
        # Metered resellers pay for usage or service-hours instead of per order (TEN-05).
        q = pricing.Quote(d.base_price, d.base_price, 0, 0)
        d.params = {**d.params, "metered": True}
    if q.reseller_discount and not customer.is_reseller:
        d.params = {**d.params, "tier": pricing.reseller_percent(customer)}  # a loyalty discount (GRW-02)
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
    deleted = await session.execute(delete(DiscountRedemption).where(DiscountRedemption.order_id == order.id))
    if order.discount_code_id and affected(deleted):
        # Only decrement when we actually removed a redemption: concurrent
        # refund_failed retries share the idempotent wallet refund but must not
        # double-decrement the coupon counter.
        await session.execute(
            update(DiscountCode)
            .where(DiscountCode.id == order.discount_code_id, DiscountCode.used_count > 0)
            .values(used_count=DiscountCode.used_count - 1)
        )


async def transfer_service(
    session: AsyncSession, service_id: int, from_customer_id: int, to_customer_id: int
) -> Service:
    """Move a service from one customer to another, keeping its panel username.

    Refuses when the target already owns the service's one-buy plan.
    Router wiring lives elsewhere; this is the reusable helper.
    """
    service = await session.get(Service, service_id)
    if service is None or service.customer_id != from_customer_id:
        raise OrderError("service_not_found")
    if from_customer_id == to_customer_id:
        return service
    target = await session.get(Customer, to_customer_id)
    if target is None or target.shop_id != service.shop_id:
        raise OrderError("service_not_found")
    if service.plan_id is not None:
        plan = await session.get(Plan, service.plan_id)
        if plan is not None:
            await _ensure_one_buy(session, target, plan)
    history = list((service.panel_data or {}).get("transfers") or [])
    history.append({"from": from_customer_id, "to": to_customer_id, "at": utcnow().isoformat()})
    service.panel_data = {**(service.panel_data or {}), "transfers": history[-20:]}
    service.customer_id = to_customer_id
    await session.flush()
    return service


async def report_issue(session: AsyncSession, service_id: int, customer_id: int, text: str):  # type: ignore[no-untyped-def]
    """File a disorder/outage report for a service as a support ticket (category 'disorder')."""
    from paurubot.core import tickets as ticket_api

    service = await session.get(Service, service_id)
    customer = await session.get(Customer, customer_id)
    if service is None or customer is None:
        raise OrderError("service_not_found")
    if service.customer_id != customer.id or service.shop_id != customer.shop_id:
        raise OrderError("service_not_found")
    body = (text or "").strip()[:1000] or "-"
    ticket = await ticket_api.open_ticket(
        session, customer, f"disorder: service {service.panel_username}", source="bot"
    )
    await ticket_api.add_message(
        session,
        ticket,
        author_type="customer",
        author_id=customer.id,
        text=f"[disorder] service {service.id} ({service.panel_username}): {body}",
    )
    return ticket


async def rotate_subscription(session: AsyncSession, service_id: int) -> str:
    """Rotate a service's branded subscription link (change-link).

    Rotates the local `sub_token` so the old link stops working; the panel-side
    revoke via `provisioning.lifecycle.revoke` stays available to callers with
    panel access (it needs network), while this helper never requires it.
    """
    from paurubot.provisioning.lifecycle import new_sub_token

    service = await session.get(Service, service_id)
    if service is None:
        raise OrderError("service_not_found")
    service.sub_token = new_sub_token()
    await session.flush()
    return service.sub_token or ""


async def can_move(session: AsyncSession, customer: Customer, service: Service) -> bool:
    """Whether `customer` may move `service` under the per-user move cap.

    `move_free_limit == 0` means unlimited (default, no behavior change).
    """
    cfg = await shop_settings.load(session, customer.shop_id)
    limit = int(cfg.get("move_free_limit") or 0)
    if limit <= 0:
        return True
    if service.customer_id != customer.id:
        return False
    used = await session.scalar(
        select(func.count(Order.id)).where(
            Order.customer_id == customer.id,
            Order.kind == OrderKind.MOVE,
            Order.status.in_((OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED)),
        )
    )
    return int(used or 0) < limit
