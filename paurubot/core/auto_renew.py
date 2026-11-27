"""Opt-in auto-renewal (F-02): a service renews or tops itself up from the wallet as it runs out.

A service carries its rule in `services.auto_renew`:

* ``{"mode": "renew", "plan_id": 7}`` renews with that plan when the data or the time runs low;
* ``{"mode": "extra", "gb": 10, "days": 30}`` buys extra data when the data runs low and extra
  days when the time runs low (either one may be left out).

The worker checks every minute. Each automatic order goes through the normal order path
(draft, create, settle from the wallet), so prices, discounts, reseller rules and the provisioning
queue behave exactly as for an order the customer placed by hand. A short wallet keeps the rule on:
the customer is told once and it renews as soon as the money is there. Anything else (a plan that
is gone, extras switched off...) stops the rule and tells the customer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.worker import queue

log = logging.getLogger(__name__)

SOURCE = "auto"
LEAD = timedelta(hours=1)  # renew this long before the period ends
COOLDOWN = timedelta(minutes=30)  # after an automatic order, give the panel time to show it
LOW_FLOOR = 256 * 1024**2  # data counts as low under this many bytes...
LOW_SHARE = 0.02  # ...or under this share of the limit, whichever is larger
GB_OPTIONS = (5, 10, 20, 50)
DAY_OPTIONS = (7, 15, 30, 60)
ELIGIBLE = (ServiceStatus.ACTIVE, ServiceStatus.LIMITED, ServiceStatus.EXPIRED)
BUSY = (OrderStatus.PAID, OrderStatus.PROVISIONING)
SHORT = "insufficient_balance"
BATCH = 500


class _Usage(Protocol):
    auto_renew: dict[str, Any] | None
    status: ServiceStatus
    is_trial: bool
    data_limit: int
    used_bytes: int
    expire_at: datetime | None


def rule(service: _Usage) -> dict[str, Any] | None:
    """The service's rule, or None when it has none (or a malformed one)."""
    raw = service.auto_renew
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode == "renew" and _int(raw.get("plan_id")) > 0:
        return raw
    if mode == "extra" and (_int(raw.get("gb")) > 0 or _int(raw.get("days")) > 0):
        return raw
    return None


def is_on(service: _Usage) -> bool:
    found = rule(service)
    return found is not None and not found.get("stopped")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def low_data(service: _Usage) -> bool:
    if not service.data_limit:
        return False
    left = max(0, service.data_limit - (service.used_bytes or 0))
    return left <= max(LOW_FLOOR, int(service.data_limit * LOW_SHARE))


def ending(service: _Usage, now: datetime) -> bool:
    return service.expire_at is not None and service.expire_at <= now + LEAD


def wanted(service: _Usage, now: datetime | None = None) -> list[tuple[OrderKind, dict[str, int]]]:
    """The orders the rule asks for right now; empty when nothing is due."""
    found = rule(service)
    if found is None or found.get("stopped") or service.is_trial or service.status not in ELIGIBLE:
        return []
    now = now or utcnow()
    low = service.status == ServiceStatus.LIMITED or low_data(service)
    late = service.status == ServiceStatus.EXPIRED or ending(service, now)
    if found["mode"] == "renew":
        return [(OrderKind.RENEW, {})] if low or late else []
    out: list[tuple[OrderKind, dict[str, int]]] = []
    if low and _int(found.get("gb")) > 0:
        out.append((OrderKind.EXTRA_VOLUME, {"extra_gb": _int(found["gb"])}))
    if late and _int(found.get("days")) > 0:
        out.append((OrderKind.EXTRA_DAYS, {"extra_days": _int(found["days"])}))
    return out


# --- the customer's choice -------------------------------------------------------


async def enable(
    session: AsyncSession, service: Service, *, plan_id: int = 0, gb: int = 0, days: int = 0
) -> dict[str, Any]:
    """Turn the rule on (or change it). Raises `orders.OrderError` when it could never be ordered."""
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        raise orders.OrderError("service_not_found")
    if plan_id:
        from paurubot.core import catalog

        offered = await catalog.plans_for_service(session, customer.shop_id, customer, service)
        plan = next((p for p in offered if p.id == plan_id and p.renewable and p.delivery != "stock"), None)
        if plan is None:
            raise orders.OrderError("plan_unavailable")  # only plans this customer could renew with by hand
        await orders.draft(session, customer, OrderKind.RENEW, plan=plan, service=service)
        new: dict[str, Any] = {"mode": "renew", "plan_id": plan.id}
    else:
        current = rule(service)
        new = {"mode": "extra"}
        if current is not None and current["mode"] == "extra":
            new.update({k: _int(current.get(k)) for k in ("gb", "days") if _int(current.get(k)) > 0})
        if gb:
            await orders.draft(session, customer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=gb)
            new["gb"] = gb
        if days:
            await orders.draft(session, customer, OrderKind.EXTRA_DAYS, service=service, extra_days=days)
            new["days"] = days
        if not (new.get("gb") or new.get("days")):
            raise orders.OrderError("extras_unavailable")
    service.auto_renew = new
    await session.flush()
    return new


async def drop_part(session: AsyncSession, service: Service, part: str) -> None:
    """Remove the extra data or the extra days from an "extra" rule; the rule goes when both are gone."""
    current = rule(service)
    if current is None or current["mode"] != "extra":
        return
    left = {k: _int(current.get(k)) for k in ("gb", "days") if k != part and _int(current.get(k)) > 0}
    service.auto_renew = {"mode": "extra", **left} if left else None
    await session.flush()


async def disable(session: AsyncSession, service: Service) -> None:
    service.auto_renew = None
    await session.flush()


# --- the worker ------------------------------------------------------------------


async def due(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    after_id: int = 0,
    through_id: int | None = None,
) -> list[int]:
    """One due batch, keyset-paging candidates instead of truncating the scan.

    The caller advances after the last returned ID, including busy/short-wallet services.
    ``through_id`` bounds a worker pass to the services present when it started.
    """
    now = now or utcnow()
    cursor = after_id
    out: list[int] = []
    while len(out) < BATCH:
        query = (
            select(
                Service.id,
                Service.auto_renew,
                Service.status,
                Service.is_trial,
                Service.data_limit,
                Service.used_bytes,
                Service.expire_at,
            )
            .where(
                Service.id > cursor,
                Service.auto_renew.is_not(None),
                Service.status.in_(ELIGIBLE),
                Service.is_trial.is_(False),
            )
            .order_by(Service.id)
            .limit(BATCH)
        )
        if through_id is not None:
            query = query.where(Service.id <= through_id)
        rows = list(await session.execute(query))
        if not rows:
            break
        out.extend(row.id for row in rows if wanted(row, now))  # type: ignore[arg-type]
        cursor = rows[-1].id
    return out[:BATCH]


async def run_service(session: AsyncSession, service_id: int, now: datetime | None = None) -> str:
    """Place what the rule asks for. Returns what happened, for the logs and the tests."""
    now = now or utcnow()
    service = await session.scalar(
        select(Service)
        .where(Service.id == service_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if service is None:
        return "gone"
    wants = wanted(service, now)
    if not wants:
        return "idle"
    found = dict(rule(service) or {})
    last_at = _when(found.get("last_at"))
    if last_at is not None and now - last_at < COOLDOWN:
        return "cooldown"
    last_id = _int(found.get("last_order_id"))
    if last_id:
        last = await session.get(Order, last_id)
        if last is not None and last.status in {OrderStatus.FAILED, OrderStatus.REFUNDED}:
            return await _stop(session, service, "order_failed")
    busy = await session.scalar(
        select(Order.id).where(Order.service_id == service.id, Order.status.in_(BUSY)).limit(1)
    )
    if busy is not None:
        return "busy"  # a renewal paid by hand (or queued for later) already covers it
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        return "gone"
    plan: Plan | None = None
    if found["mode"] == "renew":
        plan = await session.get(Plan, _int(found["plan_id"]))
        if plan is None or not plan.is_active:
            return await _stop(session, service, "plan_unavailable")
    panel = await session.get(Panel, service.panel_id)
    if panel is not None and panel.national_mode:
        if found["mode"] != "renew" or plan is None or not plan.national_stock_plan_id:
            return "national_mode"
    placed: list[int] = []
    for kind, extra in wants:
        try:
            async with session.begin_nested():
                d = await orders.draft(
                    session,
                    customer,
                    kind,
                    plan=plan,
                    service=service,
                    extra_gb=extra.get("extra_gb", 0),
                    extra_days=extra.get("extra_days", 0),
                )
                order = await orders.create(session, customer, d, source=SOURCE)
                await orders.settle_from_wallet(session, order.id)
        except orders.OrderError as exc:
            await session.refresh(service)
            if exc.key == SHORT:
                return await _short(session, service, exc.params, placed)
            if exc.key == "national_mode_unavailable":
                return "national_mode"
            return await _stop(session, service, exc.key)
        placed.append(order.id)
        log.info("auto-renew: service %s got %s order %s", service.id, kind.value, order.id)
    current = dict(service.auto_renew or {})
    current.update(last_order_id=placed[-1], last_at=now.isoformat())
    current.pop("short", None)
    service.auto_renew = current
    await session.flush()
    return "placed"


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _short(session: AsyncSession, service: Service, params: dict[str, Any], placed: list[int]) -> str:
    """Not enough in the wallet: say so once, keep the rule, try again on the next round."""
    current = dict(service.auto_renew or {})
    if placed:
        current.update(last_order_id=placed[-1], last_at=utcnow().isoformat())
    if current.get("short"):
        service.auto_renew = current
        await session.flush()
        return "short"
    current["short"] = True
    service.auto_renew = current
    await queue.enqueue(
        session,
        "notify",
        "auto_renew_notice",
        {
            "service_id": service.id,
            "reason": SHORT,
            "balance": _int(params.get("balance")),
            "required": _int(params.get("required")),
        },
        shop_id=service.shop_id,
        dedupe_key=f"auto_renew:{service.id}:short:{utcnow().isoformat()}",
    )
    await session.flush()
    return "short"


async def _stop(session: AsyncSession, service: Service, reason: str) -> str:
    current = dict(service.auto_renew or {})
    if current.get("stopped"):
        return "stopped"
    current.update(stopped=reason[:48], stopped_at=utcnow().isoformat())
    current.pop("short", None)
    service.auto_renew = current
    await queue.enqueue(
        session,
        "notify",
        "auto_renew_notice",
        {"service_id": service.id, "reason": reason[:48]},
        shop_id=service.shop_id,
        dedupe_key=f"auto_renew:{service.id}:stop:{current['stopped_at']}",
    )
    await session.flush()
    log.info("auto-renew stopped for service %s: %s", service.id, reason)
    return "stopped"
