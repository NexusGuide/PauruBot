"""Mini App order checkout uses real independent PostgreSQL commits before provider I/O."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Shop,
)
from paurubot.ledger import wallet
from paurubot.payments import engine, order_checkout, saved_checkout
from paurubot.payments.base import Checkout, PaymentError
from tests import factories
from tests.security.test_payment_intents import intent_engine as intent_engine
from tests.unit.test_webapp_api import app_env as app_env
from tests.unit.test_webapp_api import h

pytestmark = pytest.mark.security


def checkout() -> Checkout:
    return Checkout(
        redirect_url="https://invoice.invalid/order",
        provider_ref="synthetic-ref",
        provider_amount=200,
        provider_currency="USD",
    )


@pytest.fixture
async def owned_order(intent_engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    monkeypatch.setattr(order_checkout, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(order_checkout, "invoice_cents", AsyncMock(return_value=200))
    async with maker() as session:
        shop = Shop(title="Durable Mini App orders")
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(
            session, shop.id, {"card_accounts": [{"number": "6037990000000000"}], "stars_rate": 1000}
        )
        customer = await factories.customer(session, shop)
        order = Order(
            shop_id=shop.id,
            customer_id=customer.id,
            kind=OrderKind.PURCHASE,
            amount=90_000,
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.add(order)
        await session.commit()
        cid, oid = customer.id, order.id
    for provider in engine.PROVIDERS.values():
        monkeypatch.setattr(provider, "enabled", AsyncMock(return_value=True))
    yield maker, cid, oid
    shop_settings.invalidate()


async def request(env, name="plisio"):  # type: ignore[no-untyped-def]
    maker, cid, oid = env
    async with maker() as caller:
        return await order_checkout.start(
            caller, await caller.get(Customer, cid), await caller.get(Order, oid), name
        )


@pytest.mark.parametrize("name", ["nowpayments", "plisio", "card", "zarinpal", "blupal", "ton"])
async def test_attempt_visible_before_create_and_replay_survives_caller_rollback(
    owned_order, monkeypatch, name
):  # type: ignore[no-untyped-def]
    maker, cid, oid = owned_order

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as observer:
            row = await observer.get(Payment, payment.id)
            order = await observer.get(Order, oid)
            assert row.status == PaymentStatus.CREATED and row.purpose == PaymentPurpose.ORDER
            assert row.meta[order_checkout.ATTEMPT_KEY]["state"] == "uncertain"
            assert row.order_id == oid and row.amount == 90_000
            assert order.expires_at == row.expires_at
            if name in {"nowpayments", "plisio"}:
                assert row.provider_amount == 200 and row.provider_currency == "USD"
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider(name), "create", remote)
    async with maker() as caller:
        customer, order = await caller.get(Customer, cid), await caller.get(Order, oid)
        shop = await caller.get(Shop, customer.shop_id)
        title = shop.title
        shop.title = "must not be committed"
        first = await order_checkout.start(caller, customer, order, name)
        pid = first.payment.id
        await caller.rollback()
    async with maker() as observer:
        row = await observer.get(Payment, pid)
        assert row.status == PaymentStatus.PENDING
        assert (await observer.get(Shop, row.shop_id)).title == title
        if name in {"nowpayments", "plisio", "zarinpal", "blupal"}:
            assert saved_checkout.action(row, utcnow(), await observer.get(Order, oid)) == "resume"
    assert (await request(owned_order, name)).payment.id == pid
    remote.assert_awaited_once()


@pytest.mark.parametrize("failure", ["gateway", "timeout", "cancel"])
async def test_ambiguous_attempt_blocks_recreation_and_method_switch(owned_order, monkeypatch, failure):  # type: ignore[no-untyped-def]
    exc = {
        "gateway": PaymentError("gateway_error"),
        "timeout": TimeoutError(),
        "cancel": asyncio.CancelledError(),
    }[failure]
    remote = AsyncMock(side_effect=exc)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    other = AsyncMock()
    monkeypatch.setattr(engine.provider("card"), "create", other)
    with pytest.raises(type(exc)):
        await request(owned_order)
    with pytest.raises(PaymentError, match="gateway_error"):
        await request(owned_order)
    with pytest.raises(PaymentError, match="payment_intent_conflict"):
        await request(owned_order, "card")
    maker, cid, oid = owned_order
    async with maker() as observer:
        rows = list(await observer.scalars(select(Payment).where(Payment.order_id == oid)))
        assert len(rows) == 1
        assert saved_checkout.action(rows[0], utcnow(), await observer.get(Order, oid)) == "review"
        assert saved_checkout.saved_payment(rows[0], utcnow(), await observer.get(Order, oid)) is None
    remote.assert_awaited_once()
    other.assert_not_awaited()


async def test_concurrent_clicks_and_method_changes_share_one_order_reservation(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    other = AsyncMock()
    monkeypatch.setattr(engine.provider("nowpayments"), "create", other)
    first = asyncio.create_task(request(owned_order))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        for name, error in [("plisio", "gateway_error"), ("nowpayments", "payment_intent_conflict")]:
            with pytest.raises(PaymentError, match=error):
                await asyncio.wait_for(request(owned_order, name), timeout=10)
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=10)
    assert (await request(owned_order)).payment.id == result.payment.id
    remote.assert_awaited_once()
    other.assert_not_awaited()


@pytest.mark.parametrize("failed_commit", [1, 2])
async def test_commit_failure_before_and_after_acceptance(owned_order, monkeypatch, failed_commit):  # type: ignore[no-untyped-def]
    maker, cid, oid = owned_order

    class Interrupted(AsyncSession):
        commits = 0

        async def commit(self):  # type: ignore[no-untyped-def]
            self.commits += 1
            if self.commits == failed_commit:
                raise RuntimeError("synthetic commit failure")
            await super().commit()

    broken = async_sessionmaker(maker.kw["bind"], class_=Interrupted, expire_on_commit=False)
    monkeypatch.setattr(order_checkout, "get_sessionmaker", lambda: broken)
    remote = AsyncMock(return_value=checkout())
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    with pytest.raises(RuntimeError, match="synthetic commit"):
        await request(owned_order)
    async with maker() as observer:
        assert await observer.scalar(select(func.count(Payment.id)).where(Payment.order_id == oid)) == (
            1 if failed_commit == 2 else 0
        )
    if failed_commit == 1:
        remote.assert_not_awaited()
    else:
        monkeypatch.setattr(order_checkout, "get_sessionmaker", lambda: maker)
        with pytest.raises(PaymentError, match="gateway_error"):
            await request(owned_order)
        remote.assert_awaited_once()


@pytest.mark.parametrize(
    "status", [PaymentStatus.EXPIRED, PaymentStatus.FAILED, PaymentStatus.DISPUTED, PaymentStatus.REFUNDED]
)
async def test_concurrent_terminal_payment_is_never_resurrected(owned_order, monkeypatch, status):  # type: ignore[no-untyped-def]
    maker, _, oid = owned_order

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as staff:
            row = await staff.get(Payment, payment.id)
            row.status = status
            await staff.commit()
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    for _ in range(2):
        with pytest.raises(PaymentError, match="payment_intent_closed"):
            await request(owned_order)
    async with maker() as observer:
        row = await observer.scalar(select(Payment).where(Payment.order_id == oid))
        assert row.status == status and row.provider_ref == "synthetic-ref"
    remote.assert_awaited_once()


async def test_cancelled_order_keeps_evidence_without_reopening_or_credit(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    maker, _, oid = owned_order

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as other:
            row = await other.get(Order, oid)
            row.status = OrderStatus.CANCELED
            await other.commit()
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    with pytest.raises(PaymentError, match="order_expired"):
        await request(owned_order)
    async with maker() as observer:
        assert (await observer.get(Order, oid)).status == OrderStatus.CANCELED
        row = await observer.scalar(select(Payment).where(Payment.order_id == oid))
        assert row.provider_ref == "synthetic-ref" and row.paid_at is None
    remote.assert_awaited_once()


async def test_replay_freezes_due_despite_wallet_changes_but_not_order_edits(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    maker, cid, oid = owned_order
    remote = AsyncMock(return_value=checkout())
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    first = await request(owned_order)
    async with maker() as session:
        customer = await session.get(Customer, cid)
        await wallet.topup(
            session, customer.shop_id, cid, 100_000, provider="card", payment_id=first.payment.id + 999999
        )
        await session.commit()
    again = await request(owned_order)
    assert again.payment.id == first.payment.id and again.payment.amount == first.payment.amount
    async with maker() as session:
        order = await session.get(Order, oid)
        order.amount += 1000
        await session.commit()
    with pytest.raises(PaymentError, match="payment_intent_conflict"):
        await request(owned_order)
    remote.assert_awaited_once()


async def test_existing_legacy_payment_and_foreign_ownership_fail_closed(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    maker, cid, oid = owned_order
    remote = AsyncMock()
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    async with maker() as session:
        customer = await session.get(Customer, cid)
        other_shop = Shop(title="Other shop")
        session.add(other_shop)
        await session.flush()
        other = await factories.customer(session, other_shop)
        order = await session.get(Order, oid)
        with pytest.raises(PaymentError, match="order_expired"):
            await order_checkout.start(session, other, order, "plisio")
        session.add(
            Payment(
                shop_id=customer.shop_id,
                customer_id=cid,
                order_id=oid,
                purpose=PaymentPurpose.ORDER,
                provider="card",
                amount=90_000,
                idempotency_key=f"legacy:{oid}",
            )
        )
        await session.commit()
    with pytest.raises(PaymentError, match="payment_intent_conflict"):
        await request(owned_order)
    remote.assert_not_awaited()


async def test_actual_card_metadata_and_instructions_are_retained(owned_order):  # type: ignore[no-untyped-def]
    first = await request(owned_order, "card")
    again = await request(owned_order, "card")
    assert first.payment.id == again.payment.id
    assert first.checkout == again.checkout
    assert again.checkout.instructions["accounts"][0]["number"] == "6037990000000000"
    assert again.payment.meta["card"] == "0000"


async def test_frozen_hosted_conversion_is_consumed(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    quote = AsyncMock(side_effect=AssertionError("no re-quote after commit"))
    monkeypatch.setattr("paurubot.payments.providers.invoices.invoice_cents", quote)

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        assert await engine.provider("plisio")._cents(payment, cfg) == 200
        return checkout()

    monkeypatch.setattr(engine.provider("plisio"), "create", AsyncMock(side_effect=create))
    await request(owned_order)
    quote.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["expired", "cancelled", "funded", "dirty"])
async def test_no_attempt_for_invalid_or_uncommitted_order(owned_order, monkeypatch, invalid):  # type: ignore[no-untyped-def]
    maker, cid, oid = owned_order
    remote = AsyncMock()
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    async with maker() as caller:
        customer, order = await caller.get(Customer, cid), await caller.get(Order, oid)
        if invalid == "expired":
            order.expires_at = utcnow() - timedelta(seconds=1)
        elif invalid == "cancelled":
            order.status = OrderStatus.CANCELED
        elif invalid == "funded":
            await wallet.topup(
                caller, customer.shop_id, cid, 100_000, provider="card", payment_id=oid + 9999999
            )
        else:
            order.amount += 1000
        if invalid != "dirty":
            await caller.commit()
        with pytest.raises(PaymentError):
            await order_checkout.start(caller, customer, order, "plisio")
        await caller.rollback()
    async with maker() as observer:
        assert await observer.scalar(select(func.count(Payment.id)).where(Payment.order_id == oid)) == 0
    remote.assert_not_awaited()


async def test_api_card_order_replay_and_auth_scope(session, app_env, monkeypatch):  # type: ignore[no-untyped-def]
    client, plan = app_env["client"], app_env["plan"]
    created = await client.post(
        "/api/webapp/orders", headers=h(89101), json={"kind": "purchase", "plan_id": plan.id}
    )
    oid = created.json()["order"]["id"]
    path = f"/api/webapp/orders/{oid}/pay"
    remote = AsyncMock(wraps=engine.provider("card").create)
    monkeypatch.setattr(engine.provider("card"), "create", remote)
    assert (await client.post(path, json={"method": "card"})).status_code == 401
    assert (await client.post(path, headers=h(89102), json={"method": "card"})).status_code == 404
    first = await client.post(path, headers=h(89101), json={"method": "card"})
    again = await client.post(path, headers=h(89101), json={"method": "card"})
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert first.json()["payment"] == again.json()["payment"]
    pid = first.json()["payment"]["payment_id"]
    assert "meta" not in first.text and "order_attempt_v1" not in first.text
    conflict = await client.post(path, headers=h(89101), json={"method": "stars"})
    assert conflict.status_code == 409 and conflict.json()["error"] == "payment_intent_conflict"
    row = await session.get(Payment, pid)
    assert row.purpose == PaymentPurpose.ORDER and row.paid_at is None
    remote.assert_awaited_once()


async def test_stars_uses_shop_bot_and_replays_the_same_invoice_link(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    from paurubot.notifications import telegram

    bot = AsyncMock()
    bot.create_invoice_link.return_value = "https://t.me/$synthetic-order-invoice"
    choose_bot = AsyncMock(return_value=bot)
    monkeypatch.setattr(telegram, "bot_for", choose_bot)
    first = await request(owned_order, "stars")
    again = await request(owned_order, "stars")
    assert first.payment.id == again.payment.id
    assert first.checkout == again.checkout
    assert first.checkout.instructions["invoice_link"] == "https://t.me/$synthetic-order-invoice"
    choose_bot.assert_awaited_once_with(first.payment.shop_id)
    bot.create_invoice_link.assert_awaited_once()
    assert bot.create_invoice_link.call_args.kwargs["payload"] == f"pay:{first.payment.id}"


async def test_stars_ambiguous_link_creation_is_not_retried(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    from paurubot.notifications import telegram

    bot = AsyncMock()
    bot.create_invoice_link.side_effect = TimeoutError()
    monkeypatch.setattr(telegram, "bot_for", AsyncMock(return_value=bot))
    with pytest.raises(TimeoutError):
        await request(owned_order, "stars")
    with pytest.raises(PaymentError, match="gateway_error"):
        await request(owned_order, "stars")
    bot.create_invoice_link.assert_awaited_once()


async def test_simultaneous_first_requests_have_one_creator(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    barrier = asyncio.Barrier(4)
    remote = AsyncMock(return_value=checkout())
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)

    async def click():  # type: ignore[no-untyped-def]
        await barrier.wait()
        try:
            return (await request(owned_order)).payment.id
        except PaymentError as exc:
            assert exc.key == "gateway_error"  # another creator is still finishing
            return None

    results = await asyncio.wait_for(asyncio.gather(*(click() for _ in range(4))), timeout=20)
    ids = {pid for pid in results if pid is not None}
    assert len(ids) == 1
    assert (await request(owned_order)).payment.id in ids
    remote.assert_awaited_once()


async def test_payment_expiring_during_create_is_not_reopened(owned_order, monkeypatch):  # type: ignore[no-untyped-def]
    maker, _, oid = owned_order

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as other:
            row = await other.get(Payment, payment.id)
            row.expires_at = utcnow() - timedelta(seconds=1)
            await other.commit()
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    with pytest.raises(PaymentError, match="payment_intent_closed"):
        await request(owned_order)
    async with maker() as observer:
        row = await observer.scalar(select(Payment).where(Payment.order_id == oid))
        assert row.status == PaymentStatus.EXPIRED and row.provider_ref == "synthetic-ref"
    remote.assert_awaited_once()
