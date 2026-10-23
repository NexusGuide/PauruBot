"""Read-only renewal estimate using the worker's own target calculator.

No panel I/O, persisted target, price change, payment or capability/policy override.
The worker recalculates from its execution-time snapshot, so this is not a promise.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Plan, Service, ServiceStatus
from paurubot.provisioning import lifecycle, renewal_state


def _snapshot(expire: str | None, limit: int, used: int) -> dict[str, Any]:
    return {
        "expire_at": expire,
        "data_limit": limit,
        "used_bytes": used,
        "remaining_bytes": max(0, limit - used) if limit else None,
    }


async def for_order(session: AsyncSession, customer: Customer, order: Order) -> dict[str, Any]:
    if order.shop_id != customer.shop_id or order.customer_id != customer.id:
        raise orders.OrderError("order_not_found")
    now = utcnow()
    if order.status != OrderStatus.PENDING or (order.expires_at and order.expires_at <= now):
        raise orders.OrderError("order_expired")
    if order.params.get("national") or order.kind != OrderKind.RENEW:
        raise orders.OrderError("renewal_preview_unavailable")
    service = (
        await session.get(Service, order.service_id, populate_existing=True) if order.service_id else None
    )
    if (
        service is None
        or service.shop_id != customer.shop_id
        or service.customer_id != customer.id
        or service.status == ServiceStatus.DELETED
    ):
        raise orders.OrderError("service_not_found")
    plan = await session.get(Plan, order.plan_id, populate_existing=True) if order.plan_id else None
    if (
        service.is_trial
        or plan is None
        or not plan.renewable
        or plan.delivery == "stock"
        or (plan.panel_id and plan.panel_id != service.panel_id)
        or not orders.can_change(service, "renew")
        or (service.panel and (service.panel.options or {}).get("allow_renewals") is False)
    ):
        raise orders.OrderError("renewal_preview_unavailable")
    predecessor = await session.scalar(
        select(Order.id)
        .where(
            Order.service_id == service.id,
            Order.id != order.id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
            Order.kind.in_([*renewal_state.KINDS, OrderKind.MOVE]),
        )
        .limit(1)
    )
    if predecessor is not None:
        raise orders.OrderError("renewal_preview_busy")
    cfg = await shop_settings.load(session, order.shop_id)
    mode = lifecycle.renew_mode(plan, service.panel, cfg)
    queued_until = await orders.queued_until(session, order)
    # This is the SAME calculator used by _modify_service, never a UI-side fork.
    target: dict[str, Any] = lifecycle._target_for(
        service, order, plan, mode, queued=queued_until is not None
    )
    before = _snapshot(
        service.expire_at.isoformat() if service.expire_at else None,
        int(service.data_limit or 0),
        int(service.used_bytes or 0),
    )
    after = None
    if queued_until is None:
        limit = int(target["limit"]) if target["limit"] is not None else before["data_limit"]
        used = 0 if target["reset"] else before["used_bytes"]
        after = _snapshot(target["expire"] if target["set_expire"] else before["expire_at"], limit, used)
    valid_until = now + timedelta(seconds=60)
    if order.expires_at and order.expires_at < valid_until:
        valid_until = order.expires_at
    return {
        "order_id": order.id,
        "service_id": service.id,
        "plan_id": plan.id,
        "amount": order.amount,
        "computed_at": now.isoformat(),
        "valid_until": valid_until.isoformat(),
        "basis": "stored_service_snapshot",
        "timing": "queued" if queued_until else "immediate",
        "queued_until": queued_until.isoformat() if queued_until else None,
        "policy": target["policy"],
        "before": before,
        "after": after,
        "resets_usage": bool(target["reset"]),
    }
