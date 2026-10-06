from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.ledger import posting, wallet
from paurubot.ledger.accounts import system_account, wallet_account, wallet_balance
from tests import factories


async def test_topup_charge_refund_cycle(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 150_000, provider="card", payment_id=1)
    await wallet.charge(session, shop.id, cust.id, 100_000, order_id=7)
    assert await wallet_balance(session, shop.id, cust.id) == 50_000
    await wallet.refund(session, shop.id, cust.id, 100_000, order_id=7)
    assert await wallet_balance(session, shop.id, cust.id) == 150_000
    revenue = await system_account(session, shop.id, "revenue")
    assert revenue.balance == 0
    assert await posting.reconcile(session, shop.id) == []


async def test_postings_are_idempotent(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    first = await wallet.topup(session, shop.id, cust.id, 10_000, provider="card", payment_id=99)
    second = await wallet.topup(session, shop.id, cust.id, 10_000, provider="card", payment_id=99)
    assert first.created and not second.created
    assert first.journal.id == second.journal.id
    assert await wallet_balance(session, shop.id, cust.id) == 10_000


async def test_wallet_cannot_go_negative_and_failure_leaves_no_trace(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 5_000, provider="card", payment_id=3)
    with pytest.raises(posting.InsufficientFunds):
        await wallet.charge(session, shop.id, cust.id, 6_000, order_id=11)
    assert await wallet_balance(session, shop.id, cust.id) == 5_000
    # the rejected journal was rolled back, so the same key can be used later
    await wallet.topup(session, shop.id, cust.id, 5_000, provider="card", payment_id=4)
    posted = await wallet.charge(session, shop.id, cust.id, 6_000, order_id=11)
    assert posted is not None and posted.created
    assert await posting.reconcile(session, shop.id) == []


async def test_unbalanced_and_cross_shop_journals_rejected(session) -> None:  # type: ignore[no-untyped-def]
    shop_a = await factories.shop(session, "A")
    shop_b = await factories.shop(session, "B")
    cust = await factories.customer(session, shop_a)
    wallet_a = await wallet_account(session, shop_a.id, cust.id)
    revenue_b = await system_account(session, shop_b.id, "revenue")
    with pytest.raises(posting.LedgerError):
        await posting.post(
            session, shop_id=shop_a.id, kind="x", idempotency_key="k1", lines=[posting.Line(wallet_a.id, 10)]
        )
    with pytest.raises(posting.LedgerError):
        await posting.transfer(
            session,
            shop_id=shop_a.id,
            source_id=revenue_b.id,
            target_id=wallet_a.id,
            amount=10,
            kind="x",
            idempotency_key="k2",
        )


async def test_reverse_restores_balances(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    posted = await wallet.adjust(session, shop.id, cust.id, 25_000, key="adj-1", actor="staff:1")
    await posting.reverse(session, posted.journal.id, idempotency_key="adj-1:reverse")
    assert await wallet_balance(session, shop.id, cust.id) == 0
    assert await posting.reconcile(session, shop.id) == []


async def test_concurrent_charges_never_overdraw(engine) -> None:  # type: ignore[no-untyped-def]
    """Ten parallel purchases against a balance that covers only three."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Concurrency")
        cust = await factories.customer(s, shop)
        await wallet.topup(s, shop.id, cust.id, 30_000, provider="card", payment_id=500)
        await s.commit()
        shop_id, cust_id = shop.id, cust.id

    async def buy(order_id: int) -> bool:
        async with maker() as s2:
            try:
                await wallet.charge(s2, shop_id, cust_id, 10_000, order_id=order_id)
                await s2.commit()
                return True
            except posting.InsufficientFunds:
                await s2.rollback()
                return False

    results = await asyncio.gather(*(buy(9000 + i) for i in range(10)))
    assert sum(results) == 3
    async with maker() as s3:
        assert await wallet_balance(s3, shop_id, cust_id) == 0
        assert await posting.reconcile(s3, shop_id) == []
