"""SEC-F18: two open on-chain payments to one address never ask for the same amount.

The extra base units came from the payment ID alone (ID modulo the slot count). Payments whose IDs
differ by a multiple of it, or with different bases that land on the same value, asked for the
same amount. One transfer then matched both and the money waited for review. Slots are now
reserved under a lock on the network and address.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus
from paurubot.payments import engine as payments
from paurubot.payments.base import PaymentError
from paurubot.payments.providers import crypto
from tests import factories

TRON = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"
SETTINGS = {
    "crypto_enabled": True,
    "crypto_networks": ["usdt_trc20"],
    "tron_address": TRON,
    "fx_source": "manual",
    "usdt_rate": 100_000,
}


async def _shop(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, SETTINGS)
    return shop


async def _start(session, cust, amount: int = 200_000):  # type: ignore[no-untyped-def]
    started = await payments.start(session, cust, "usdt_trc20", amount)
    await session.commit()
    return started.payment


async def test_ids_that_share_a_slot_still_get_different_amounts(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(crypto, "DUST_SLOTS", 3)
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    cust_id = cust.id
    first, second, third = [await _start(session, cust) for _ in range(3)]
    held = [first.provider_amount, second.provider_amount, third.provider_amount]
    second_id = second.id
    assert len(set(held)) == 3
    # A fourth payment's natural slot repeats the first one's; every slot is held: refuse.
    with pytest.raises(PaymentError, match="method_busy"):
        await payments.start(session, cust, "usdt_trc20", 200_000)
    await session.rollback()
    # A payment that can no longer be matched (expired beyond the window) frees its slot.
    await session.execute(
        update(Payment)
        .where(Payment.id == second_id)
        .values(status=PaymentStatus.EXPIRED, created_at=utcnow() - timedelta(hours=25))
    )
    await session.commit()
    fourth = await _start(session, await session.get(Customer, cust_id))
    assert fourth.provider_amount == held[1]
    assert fourth.provider_amount not in {held[0], held[2]}


async def test_shops_sharing_an_address_share_its_slots(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(crypto, "DUST_SLOTS", 1)
    one, two = await _shop(session), await _shop(session)
    await _start(session, await factories.customer(session, one))
    with pytest.raises(PaymentError, match="method_busy"):
        await payments.start(session, await factories.customer(session, two), "usdt_trc20", 200_000)


async def test_concurrent_payments_cannot_take_the_same_slot(engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(crypto, "DUST_SLOTS", 1)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Crypto race")
        await shop_settings.set_many(
            s, shop.id, {**SETTINGS, "tron_address": "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"}
        )
        customer_ids = [(await factories.customer(s, shop)).id for _ in range(4)]
        await s.commit()

    async def start(customer_id: int) -> int | None:
        async with maker() as s2:
            customer = await s2.get(Customer, customer_id)
            try:
                started = await payments.start(s2, customer, "usdt_trc20", 200_000)
                await asyncio.sleep(0.05)  # keep the transaction (and its lock) open a moment
                await s2.commit()
                return started.payment.provider_amount
            except PaymentError:
                await s2.rollback()
                return None

    results = await asyncio.gather(*(start(cid) for cid in customer_ids))
    assert len([r for r in results if r is not None]) == 1  # one slot, one payment
