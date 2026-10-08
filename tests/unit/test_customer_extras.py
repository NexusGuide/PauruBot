"""Wallet transfers (FIN-08) and referral limits (GRW-09)."""

from __future__ import annotations

import pytest

from paurubot.core import settings as shop_settings
from paurubot.core import transfers
from paurubot.db.models import Order, OrderKind, OrderStatus
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.provisioning.rewards import reward_referrer
from tests import factories


@pytest.fixture
async def shop(session):  # type: ignore[no-untyped-def]
    row = await factories.shop(session, "Extras")
    await shop_settings.set_many(
        session,
        row.id,
        {
            "wallet_transfer_enabled": True,
            "wallet_transfer_min": 10_000,
            "wallet_transfer_daily_max": 100_000,
        },
    )
    await session.commit()
    return row


async def test_transfer_moves_money_once_and_respects_limits(session, shop) -> None:  # type: ignore[no-untyped-def]
    alice = await factories.customer(session, shop, username="alice")
    bob = await factories.customer(session, shop, username="bob_b")
    await wallet.topup(session, shop.id, alice.id, 200_000, provider="card", payment_id=alice.id)
    assert (await transfers.find_recipient(session, alice, "@BOB_B")).id == bob.id
    assert (await transfers.find_recipient(session, alice, str(bob.telegram_id))).id == bob.id
    for bad in ("@nobody", "12", f"{alice.telegram_id}"):
        with pytest.raises(transfers.TransferError):
            await transfers.find_recipient(session, alice, bad)
    await transfers.send(session, alice, bob, 60_000, key="a1")
    await transfers.send(session, alice, bob, 60_000, key="a1")  # the same button twice
    assert await wallet_balance(session, shop.id, alice.id) == 140_000
    assert await wallet_balance(session, shop.id, bob.id) == 60_000
    with pytest.raises(transfers.TransferError) as small:
        await transfers.send(session, alice, bob, 5_000, key="a2")
    assert small.value.key == "transfer_min"
    with pytest.raises(transfers.TransferError) as daily:
        await transfers.send(session, alice, bob, 50_000, key="a3")  # 60k + 50k > 100k a day
    assert daily.value.key == "transfer_daily_max"
    with pytest.raises(transfers.TransferError) as broke:
        await transfers.send(session, bob, alice, 70_000, key="b1")
    assert broke.value.key == "insufficient_balance"
    other = await factories.shop(session, "Other shop")
    stranger = await factories.customer(session, other)
    with pytest.raises(transfers.TransferError):
        await transfers.send(session, alice, stranger, 10_000, key="a4")
    await shop_settings.set_many(session, shop.id, {"wallet_transfer_enabled": False})
    with pytest.raises(transfers.TransferError) as off:
        await transfers.send(session, alice, bob, 10_000, key="a5")
    assert off.value.key == "transfer_off"


async def _order(session, shop, customer, amount):  # type: ignore[no-untyped-def]
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        base_price=amount,
        amount=amount,
        params={},
    )
    session.add(order)
    await session.flush()
    return order


async def test_referral_minimum_and_daily_cap(session, shop) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, shop.id, {"referral_percent": 10, "referral_min_order": 50_000, "referral_daily_max": 1}
    )
    inviter = await factories.customer(session, shop)
    first = await factories.customer(session, shop)
    second = await factories.customer(session, shop)
    first.referrer_id = second.referrer_id = inviter.id
    await session.flush()
    assert await reward_referrer(session, await _order(session, shop, first, 20_000)) == 0  # too small
    assert not first.referral_rewarded  # a later, larger first purchase still counts
    assert await reward_referrer(session, await _order(session, shop, first, 100_000)) == 10_000
    assert await reward_referrer(session, await _order(session, shop, second, 100_000)) == 0  # cap: 1 a day
    assert await wallet_balance(session, shop.id, inviter.id) == 10_000
    inviter.is_banned = True
    await shop_settings.set_many(session, shop.id, {"referral_daily_max": 0})
    assert await reward_referrer(session, await _order(session, shop, second, 100_000)) == 0
