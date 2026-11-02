"""SEC-F02: per-customer and first-purchase discount limits hold at settlement.

A code is validated when the order is created. Several unpaid orders created before any is paid
all passed that check, so the limits are decided again, under a lock, when each order is paid.
The quoted price itself is honoured until the order expires, even if the code expired meanwhile.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import orders, pricing
from paurubot.db.models import Customer, DiscountCode, DiscountRedemption, Order, OrderKind, OrderStatus
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from tests import factories


async def _setup(session, **code_fields):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000)
    cust = await factories.customer(session, shop)
    code = DiscountCode(shop_id=shop.id, code="LIMIT", value=code_fields.pop("value", 20), **code_fields)
    session.add(code)
    await session.flush()
    await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
    return shop, plan, cust, code


async def _order(session, cust, plan, code=None):  # type: ignore[no-untyped-def]
    draft = await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    return await orders.create(session, cust, draft, code=code)


async def test_per_customer_limit_holds_across_unpaid_orders(session) -> None:  # type: ignore[no-untyped-def]
    # per_customer_limit defaults to 1: every ordinary code is one per customer.
    shop, plan, cust, code = await _setup(session)
    first = await _order(session, cust, plan, code)
    second = await _order(session, cust, plan, code)  # both passed validation: nothing redeemed yet
    assert first.amount == second.amount == 80_000
    await orders.settle_from_wallet(session, first.id)
    before = await wallet_balance(session, shop.id, cust.id)
    with pytest.raises(orders.OrderError, match="discount_already_used"):
        await orders.settle_from_wallet(session, second.id)
    assert await wallet_balance(session, shop.id, cust.id) == before  # the charge was rolled back
    await session.refresh(second)
    assert second.status == OrderStatus.PENDING
    assert code.used_count == 1


async def test_first_purchase_code_cannot_be_paid_twice(session) -> None:  # type: ignore[no-untyped-def]
    shop, plan, cust, code = await _setup(session, first_purchase_only=True, per_customer_limit=0)
    first = await _order(session, cust, plan, code)
    second = await _order(session, cust, plan, code)
    await orders.settle_from_wallet(session, first.id)  # paid, not yet completed
    with pytest.raises(orders.OrderError, match="discount_first_purchase_only"):
        await orders.settle_from_wallet(session, second.id)
    # A new order with the code is refused already at creation now.
    with pytest.raises(pricing.DiscountError, match="discount_first_purchase_only"):
        await _order(session, cust, plan, code)


async def test_free_first_purchase_code_counts_as_a_purchase(session) -> None:  # type: ignore[no-untyped-def]
    shop, plan, cust, code = await _setup(session, first_purchase_only=True, value=100, per_customer_limit=0)
    first = await _order(session, cust, plan, code)
    assert first.amount == 0
    await orders.settle_from_wallet(session, first.id)
    with pytest.raises(pricing.DiscountError, match="discount_first_purchase_only"):
        await _order(session, cust, plan, code)


async def test_a_paid_order_without_a_code_also_ends_the_first_purchase(session) -> None:  # type: ignore[no-untyped-def]
    shop, plan, cust, code = await _setup(session, first_purchase_only=True, per_customer_limit=0)
    with_code = await _order(session, cust, plan, code)
    plain = await _order(session, cust, plan)
    await orders.settle_from_wallet(session, plain.id)
    with pytest.raises(orders.OrderError, match="discount_first_purchase_only"):
        await orders.settle_from_wallet(session, with_code.id)


async def test_quoted_price_is_honoured_after_the_code_expires(session) -> None:  # type: ignore[no-untyped-def]
    shop, plan, cust, code = await _setup(session, per_customer_limit=1)
    order = await _order(session, cust, plan, code)
    code.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    code.is_active = False
    await session.flush()
    paid = await orders.settle_from_wallet(session, order.id)
    assert paid.status == OrderStatus.PAID and paid.amount == 80_000


async def test_codes_without_customer_limits_are_unchanged(session) -> None:  # type: ignore[no-untyped-def]
    shop, plan, cust, code = await _setup(session, per_customer_limit=0)
    for _ in range(3):
        order = await _order(session, cust, plan, code)
        await orders.settle_from_wallet(session, order.id)
    assert code.used_count == 3


async def test_concurrent_settlements_of_a_free_code_redeem_once(engine) -> None:  # type: ignore[no-untyped-def]
    """Real separate transactions; a 100% code charges nothing, so no wallet charge serializes it."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Discount race")
        panel = await factories.panel(s, shop)
        plan = await factories.plan(s, shop, panel, price=100_000)
        cust = await factories.customer(s, shop)
        code = DiscountCode(shop_id=shop.id, code="FREE1", value=100, per_customer_limit=1)
        s.add(code)
        await s.flush()
        order_ids = [(await _order(s, cust, plan, code)).id for _ in range(4)]
        await s.commit()
        code_id = code.id

    async def pay(order_id: int) -> bool:
        async with maker() as s2:
            try:
                await orders.settle_from_wallet(s2, order_id)
                await s2.commit()
                return True
            except orders.OrderError:
                await s2.rollback()
                return False

    results = await asyncio.gather(*(pay(order_id) for order_id in order_ids))
    assert sum(results) == 1
    async with maker() as s3:
        redeemed = await s3.scalar(
            select(func.count(DiscountRedemption.id)).where(DiscountRedemption.code_id == code_id)
        )
        paid = await s3.scalar(
            select(func.count(Order.id)).where(Order.id.in_(order_ids), Order.status == OrderStatus.PAID)
        )
        assert redeemed == 1 and paid == 1
        assert (await s3.get(Customer, cust.id)) is not None
