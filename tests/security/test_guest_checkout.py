"""Guest reservation and payment attempt commit independently before remote I/O."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import guest, guest_checkout
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, GuestCheckoutIntent, Order, Payment, Shop
from paurubot.payments import engine, order_checkout, saved_checkout
from paurubot.payments.base import Checkout, PaymentError
from paurubot.security import guest_form
from tests import factories
from tests.security.test_payment_intents import intent_engine as intent_engine

pytestmark = pytest.mark.security


@pytest.fixture
async def guest_env(intent_engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    monkeypatch.setattr(guest_checkout, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(order_checkout, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(order_checkout, "invoice_cents", AsyncMock(return_value=200))
    async with maker() as session:
        shop = await factories.shop(session, "Durable guest shop")
        await shop_settings.set_many(session, shop.id, {"guest_buy_enabled": True})
        panel = await factories.panel(session, shop)
        plan = await factories.plan(session, shop, panel, price=75_000)
        await session.commit()
        sid, pid = shop.id, plan.id
    for name in guest.ONLINE_METHODS:
        monkeypatch.setattr(engine.provider(name), "enabled", AsyncMock(return_value=True))
    yield maker, sid, pid, guest_form.issue(sid)
    shop_settings.invalidate()


def invoice() -> Checkout:
    return Checkout(redirect_url="https://invoice.invalid/guest", provider_ref="guest-synthetic-ref")


async def start(env, *, plan_id=None, method="aqayepardakht"):  # type: ignore[no-untyped-def]
    _, sid, pid, token = env
    return await guest_checkout.start(sid, pid if plan_id is None else plan_id, method, "fa", token)


async def counts(env):  # type: ignore[no-untyped-def]
    maker, sid, _, _ = env
    async with maker() as observer:
        return tuple(
            [
                await observer.scalar(select(func.count()).select_from(model).where(model.shop_id == sid))
                for model in (Customer, Order, Payment, GuestCheckoutIntent)
            ]
        )


async def test_pre_call_visibility_replay_and_frozen_selection(guest_env, monkeypatch):  # type: ignore[no-untyped-def]
    maker, sid, pid, token = guest_env

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        assert await counts(guest_env) == (1, 1, 1, 1)
        async with maker() as observer:
            row = await observer.get(GuestCheckoutIntent, guest_form.digest(token))
            assert row.order_id == payment.order_id and row.customer_id == payment.customer_id
            assert row.plan_id == pid and row.method == "aqayepardakht"
            stored = await observer.get(Payment, payment.id)
            assert stored.meta[order_checkout.ATTEMPT_KEY]["state"] == "uncertain"
        return invoice()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    first = await start(guest_env)
    assert first == await start(guest_env)
    with pytest.raises(guest.GuestError, match="payment_intent_conflict"):
        await start(guest_env, plan_id=pid + 999999)
    with pytest.raises(guest.GuestError, match="payment_intent_conflict"):
        await start(guest_env, method="plisio")
    assert await counts(guest_env) == (1, 1, 1, 1)
    remote.assert_awaited_once()


@pytest.mark.parametrize("failure", ["gateway", "timeout", "cancel"])
async def test_ambiguous_attempt_is_not_recreated(guest_env, monkeypatch, failure):  # type: ignore[no-untyped-def]
    exc = {
        "gateway": PaymentError("gateway_error"),
        "timeout": TimeoutError(),
        "cancel": asyncio.CancelledError(),
    }[failure]
    remote = AsyncMock(side_effect=exc)
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await start(guest_env)
    else:
        assert await start(guest_env)
    assert await start(guest_env)
    assert await counts(guest_env) == (1, 1, 1, 1)
    maker, sid, _, _ = guest_env
    async with maker() as observer:
        payment = await observer.scalar(select(Payment).where(Payment.shop_id == sid))
        assert saved_checkout.uncertain(payment)
    remote.assert_awaited_once()


async def test_inflight_and_simultaneous_requests_share_one_provider_attempt(guest_env, monkeypatch):  # type: ignore[no-untyped-def]
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return invoice()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    first = asyncio.create_task(start(guest_env))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        rest = await asyncio.wait_for(asyncio.gather(*(start(guest_env) for _ in range(5))), timeout=10)
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=10)
    assert rest == [result] * 5
    assert await counts(guest_env) == (1, 1, 1, 1)
    remote.assert_awaited_once()


async def test_crash_between_reservation_and_payment_can_continue_same_order(guest_env, monkeypatch):  # type: ignore[no-untyped-def]
    _, sid, pid, token = guest_env
    first = await guest_checkout.reserve(sid, pid, "aqayepardakht", "fa", token)
    assert await counts(guest_env) == (1, 1, 0, 1)
    assert first == await guest_checkout.reserve(sid, pid, "aqayepardakht", "en", token)
    remote = AsyncMock(return_value=invoice())
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    assert await start(guest_env)
    assert await counts(guest_env) == (1, 1, 1, 1)
    remote.assert_awaited_once()


async def test_claim_or_cleanup_keeps_a_tombstone_not_a_new_purchase(guest_env, monkeypatch):  # type: ignore[no-untyped-def]
    remote = AsyncMock(return_value=invoice())
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    token = await start(guest_env)
    maker, sid, _, identity = guest_env
    async with maker() as session:
        real = await factories.customer(session, await session.get(Shop, sid))
        await guest.claim(session, token, real)
        await session.commit()
    with pytest.raises(guest.GuestError, match="payment_intent_closed"):
        await start(guest_env)
    async with maker() as session:
        intent = await session.get(GuestCheckoutIntent, guest_form.digest(identity))
        await session.execute(delete(Customer).where(Customer.id == intent.customer_id))
        await session.commit()
    with pytest.raises(guest.GuestError, match="payment_intent_closed"):
        await start(guest_env)
    async with maker() as observer:
        intent = await observer.get(GuestCheckoutIntent, guest_form.digest(identity))
        assert intent is not None and intent.customer_id is None
    remote.assert_awaited_once()


async def test_invalid_form_and_refused_new_selection_leave_no_reservation(guest_env):  # type: ignore[no-untyped-def]
    _, sid, pid, token = guest_env
    with pytest.raises(guest.GuestError, match="payment_intent_invalid"):
        await guest_checkout.reserve(sid + 1, pid, "aqayepardakht", "fa", token)
    with pytest.raises(guest.GuestError, match="guest_method_unavailable"):
        await start(guest_env, method="card")
    assert await counts(guest_env) == (0, 0, 0, 0)
    with pytest.raises(guest.GuestError, match="guest_plan_unavailable"):
        await start(guest_env, plan_id=pid + 999999)
    assert await counts(guest_env) == (0, 0, 0, 0)


async def test_concurrent_first_reservations_commit_one_customer_and_order(guest_env, monkeypatch):  # type: ignore[no-untyped-def]
    from paurubot.core import orders

    entered, release = asyncio.Event(), asyncio.Event()
    original = orders.draft

    async def hold(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(orders, "draft", hold)
    remote = AsyncMock(return_value=invoice())
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", remote)
    first = asyncio.create_task(start(guest_env))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert await counts(guest_env) == (0, 0, 0, 0)
        second = asyncio.create_task(start(guest_env))
        done, _ = await asyncio.wait([second], timeout=0.1)
        assert not done  # unique identity is uncommitted; second must not create another guest
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=10)
    assert results[0] == results[1]
    assert await counts(guest_env) == (1, 1, 1, 1)
    remote.assert_awaited_once()
