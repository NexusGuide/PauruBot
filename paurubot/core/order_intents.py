"""Replay Mini App order creation in its owning transaction, before repricing.

Only keyed callers opt in. This does not create invoices or promise provider
exactly-once behavior. The customer index bounds the JSON metadata lookup.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.orders import OrderError
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderStatus

META_KEY = "order_intent_v1"
KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")


def request_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


async def create_once(
    session: AsyncSession,
    customer: Customer,
    key: str,
    payload: dict[str, Any],
    create: Callable[[], Awaitable[Order]],
) -> Order:
    if not KEY.fullmatch(key):
        raise OrderError("order_intent_invalid")
    scoped = hashlib.sha256(f"order:v1:{customer.shop_id}:{customer.id}:{key}".encode()).hexdigest()
    fingerprint = request_digest(payload)
    # Held through the request's commit/rollback; another connection must wait.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": scoped})
    order = await session.scalar(
        select(Order)
        .where(
            Order.shop_id == customer.shop_id,
            Order.customer_id == customer.id,
            Order.params[META_KEY]["key"].as_string() == scoped,
        )
        .execution_options(populate_existing=True)
    )
    if order is not None:
        if (order.params or {}).get(META_KEY, {}).get("request") != fingerprint:
            raise OrderError("order_intent_conflict")
        if order.status in {OrderStatus.CANCELED, OrderStatus.FAILED, OrderStatus.REFUNDED} or (
            order.status == OrderStatus.PENDING and order.expires_at and order.expires_at <= utcnow()
        ):
            raise OrderError("order_intent_closed")
        return order
    order = await create()
    if order.customer_id != customer.id or order.shop_id != customer.shop_id:
        raise OrderError("order_intent_conflict")
    order.params = {**(order.params or {}), META_KEY: {"key": scoped, "request": fingerprint}}
    await session.flush()
    return order
