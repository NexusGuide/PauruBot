"""Buying several services of one plan at once (CAT-07).

A batch is a set of ordinary purchase orders paid together from the wallet in one transaction:
either every order is paid or none is. Each order provisions on its own (and retries on its
own), and the customer gets one message with every link when the whole batch has finished.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders, pricing, resellers
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Plan, Service
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB

FINISHED = {OrderStatus.COMPLETED, OrderStatus.FAILED, OrderStatus.REFUNDED, OrderStatus.CANCELED}


def allowed(cfg: dict[str, Any], customer: Customer) -> int:
    """How many services this customer may buy in one go; 0 when bulk buying is off for them."""
    if not cfg.get("bulk_buy_enabled"):
        return 0
    limit = max(0, int(cfg.get("bulk_buy_max") or 0))
    if cfg.get("bulk_buy_resellers_only", True):
        profile = customer.reseller_profile
        if profile is None or not profile.is_current:
            return 0
    return limit


async def quote(session: AsyncSession, customer: Customer, plan: Plan, count: int) -> tuple[int, int]:
    """(price of one, total) after the customer's own discounts."""
    draft = await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan)
    one = pricing.quote(draft.base_price, customer, None).amount
    if resellers.metered(customer):
        one = 0
    return one, one * count


async def _check_limits(session: AsyncSession, customer: Customer, plan: Plan, count: int) -> None:
    profile = customer.reseller_profile
    if profile is None or not profile.is_current:
        return
    cap = profile.max_active_services or (profile.group.max_active_services if profile.group else None)
    if cap:
        live = await session.scalar(
            select(func.count(Service.id)).where(
                Service.customer_id == customer.id, Service.status.in_(resellers.LIVE)
            )
        )
        if int(live or 0) + count > cap:
            raise orders.OrderError("reseller_service_limit", limit=cap)
    if profile.max_total_gb and plan.data_gb > 0:
        allotted = await session.scalar(
            select(func.coalesce(func.sum(Service.data_limit), 0)).where(
                Service.customer_id == customer.id, Service.status.in_(resellers.LIVE)
            )
        )
        if int(allotted or 0) + count * plan.data_gb * GB > profile.max_total_gb * GB:
            raise orders.OrderError("reseller_volume_limit", limit=profile.max_total_gb)


async def buy_many(
    session: AsyncSession, customer: Customer, plan: Plan, count: int, label: str | None = None
) -> tuple[str, list[Order]]:
    """Create and pay `count` purchases of `plan` from the wallet. Caller commits (or rolls back)."""
    cfg = await shop_settings.load(session, customer.shop_id)
    limit = allowed(cfg, customer)
    if limit < 2:
        raise orders.OrderError("bulk_unavailable")
    if count < 2 or count > limit:
        raise orders.OrderError("bulk_count", low=2, high=limit)
    await _check_limits(session, customer, plan, count)
    _one, total = await quote(session, customer, plan, count)
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    if balance < total:
        raise orders.OrderError("insufficient_balance", balance=balance, required=total)
    batch = secrets.token_hex(6)
    base = (label or plan.title)[:40]
    made: list[Order] = []
    for number in range(1, count + 1):
        draft = await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan, label=f"{base} {number}")
        draft.params = {**draft.params, "batch": batch, "batch_size": count}
        order = await orders.create(session, customer, draft, source="bulk")
        made.append(await orders.settle_from_wallet(session, order.id))
    return batch, made


async def progress(session: AsyncSession, customer_id: int, batch: str) -> tuple[list[Order], bool]:
    rows = list(
        await session.scalars(
            select(Order)
            .where(Order.customer_id == customer_id, Order.params["batch"].as_string() == batch)
            .order_by(Order.id)
            .execution_options(populate_existing=True)
        )
    )
    return rows, bool(rows) and all(o.status in FINISHED for o in rows)
