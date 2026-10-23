"""Hosted checkout crash windows use actual independent PostgreSQL commits."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import settings as shop_settings
from paurubot.db import session as db_session
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus, Shop
from paurubot.payments import engine
from paurubot.payments.base import Checkout, PaymentError
from paurubot.payments.durable_checkout import ATTEMPT_KEY
from tests import factories
from tests.security.test_payment_intents import intent_engine as intent_engine

pytestmark = pytest.mark.security
KEY = "durable-hosted-intent-1"


@pytest.fixture
async def hosted(intent_engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    monkeypatch.setattr("paurubot.payments.durable_checkout.get_sessionmaker", lambda: maker)
    async with maker() as session:
        shop = Shop(title="Durable hosted checkout")
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(session, shop.id, {"min_topup": 1000})
        customer = await factories.customer(session, shop)
        cid = customer.id
        await session.commit()
    monkeypatch.setattr("paurubot.payments.durable_checkout.invoice_cents", AsyncMock(return_value=200))
    for name in ("nowpayments", "plisio", "tonpay"):
        monkeypatch.setattr(engine.provider(name), "enabled", AsyncMock(return_value=True))
    yield maker, cid
    shop_settings.invalidate()
    db_session.set_sessionmaker(None)  # type: ignore[arg-type]


def checkout():  # type: ignore[no-untyped-def]
    return Checkout(
        redirect_url="https://invoice.invalid/test",
        provider_ref="synthetic-ref",
        provider_amount=200,
        provider_currency="USD",
    )


@pytest.mark.parametrize("name", ["nowpayments", "plisio", "tonpay"])
async def test_attempt_visible_before_remote_call_and_survives_caller_rollback(hosted, monkeypatch, name):  # type: ignore[no-untyped-def]
    maker, cid = hosted

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as observer:
            durable = await observer.get(Payment, payment.id)
            assert durable is not None
            assert durable.status == PaymentStatus.CREATED
            assert durable.provider_amount == (100_000 if name == "tonpay" else 200)
            assert durable.provider_currency == ("IRT" if name == "tonpay" else "USD")
            assert durable.meta[ATTEMPT_KEY]["state"] == "uncertain"
        if name == "tonpay":
            return Checkout(
                redirect_url="https://tonpays.online/pay/test",
                provider_ref="synthetic-ref",
                provider_amount=payment.amount,
                provider_currency="IRT",
            )
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider(name), "create", remote)
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        shop = await caller.get(Shop, customer.shop_id)
        original_title = shop.title
        shop.title = "must roll back"
        result = await engine.start(caller, customer, name, 100_000, idempotency_key=KEY)
        pid = result.payment.id
        await caller.rollback()
    async with maker() as observer:
        durable = await observer.get(Payment, pid)
        assert durable.status == PaymentStatus.PENDING
        assert durable.provider_ref == "synthetic-ref"
        assert (await observer.get(Shop, durable.shop_id)).title == original_title
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        again = await engine.start(caller, customer, name, 100_000, idempotency_key=KEY)
        assert again.payment.id == pid
        assert again.checkout == (
            Checkout(
                redirect_url="https://tonpays.online/pay/test",
                provider_ref="synthetic-ref",
                provider_amount=100_000,
                provider_currency="IRT",
            )
            if name == "tonpay"
            else checkout()
        )
    remote.assert_awaited_once()


@pytest.mark.parametrize("name", ["nowpayments", "tonpay"])
@pytest.mark.parametrize("failure", [PaymentError("gateway_error"), asyncio.CancelledError(), TimeoutError()])
async def test_ambiguous_failure_never_sends_creation_again(hosted, monkeypatch, failure, name):  # type: ignore[no-untyped-def]
    maker, cid = hosted
    remote = AsyncMock(side_effect=failure)
    monkeypatch.setattr(engine.provider(name), "create", remote)
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(type(failure)):
            await engine.start(caller, customer, name, 100_000, idempotency_key=KEY)
        await caller.rollback()
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(PaymentError, match="gateway_error"):
            await engine.start(caller, customer, name, 100_000, idempotency_key=KEY)
        rows = list(await caller.scalars(select(Payment).where(Payment.customer_id == cid)))
        assert len(rows) == 1
        assert rows[0].meta[ATTEMPT_KEY]["state"] == "uncertain"
    remote.assert_awaited_once()


async def test_inflight_concurrent_retry_is_closed_not_a_second_post(hosted, monkeypatch):  # type: ignore[no-untyped-def]
    maker, cid = hosted
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return checkout()

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)

    async def request():  # type: ignore[no-untyped-def]
        async with maker() as caller:
            customer = await caller.get(Customer, cid)
            return await engine.start(caller, customer, "plisio", 100_000, idempotency_key=KEY)

    first = asyncio.create_task(request())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        with pytest.raises(PaymentError, match="gateway_error"):
            await asyncio.wait_for(request(), timeout=10)
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=10)
    assert (await request()).payment.id == result.payment.id
    remote.assert_awaited_once()


@pytest.mark.parametrize("status", [PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.DISPUTED])
async def test_remote_response_preserves_a_concurrent_terminal_decision(hosted, monkeypatch, status):  # type: ignore[no-untyped-def]
    maker, cid = hosted

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as staff:
            row = await staff.get(Payment, payment.id)
            row.status = status
            await staff.commit()
        return checkout()

    monkeypatch.setattr(engine.provider("nowpayments"), "create", AsyncMock(side_effect=create))
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(PaymentError, match="payment_intent_closed"):
            await engine.start(caller, customer, "nowpayments", 100_000, idempotency_key=KEY)
    async with maker() as observer:
        row = await observer.scalar(select(Payment).where(Payment.customer_id == cid))
        assert row.status == status
        assert row.provider_ref == "synthetic-ref"  # evidence retained, no resurrection/credit


async def test_expiry_during_remote_call_is_not_resurrected(hosted, monkeypatch):  # type: ignore[no-untyped-def]
    maker, cid = hosted

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        async with maker() as other:
            row = await other.get(Payment, payment.id)
            row.expires_at = utcnow() - timedelta(seconds=1)
            await other.commit()
        return checkout()

    monkeypatch.setattr(engine.provider("plisio"), "create", AsyncMock(side_effect=create))
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(PaymentError, match="payment_intent_closed"):
            await engine.start(caller, customer, "plisio", 100_000, idempotency_key=KEY)
    async with maker() as observer:
        row = await observer.scalar(select(Payment).where(Payment.customer_id == cid))
        assert row.status == PaymentStatus.EXPIRED


async def test_invalid_amount_creates_no_durable_attempt(hosted, monkeypatch):  # type: ignore[no-untyped-def]
    maker, cid = hosted
    remote = AsyncMock()
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(PaymentError, match="amount_out_of_range"):
            await engine.start(caller, customer, "plisio", -1, idempotency_key=KEY)
    async with maker() as observer:
        assert await observer.scalar(select(func.count(Payment.id)).where(Payment.customer_id == cid)) == 0
    remote.assert_not_awaited()


@pytest.mark.parametrize("failed_commit", [1, 2])
async def test_commit_failure_before_or_after_external_acceptance(hosted, monkeypatch, failed_commit):  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import AsyncSession

    maker, cid = hosted
    bind = maker.kw["bind"]

    class Interrupted(AsyncSession):
        commits = 0

        async def commit(self):  # type: ignore[no-untyped-def]
            self.commits += 1
            if self.commits == failed_commit:
                raise RuntimeError("synthetic DB connection loss")
            await super().commit()

    interrupted = async_sessionmaker(bind, class_=Interrupted, expire_on_commit=False)
    monkeypatch.setattr("paurubot.payments.durable_checkout.get_sessionmaker", lambda: interrupted)
    remote = AsyncMock(return_value=checkout())
    monkeypatch.setattr(engine.provider("plisio"), "create", remote)
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        with pytest.raises(RuntimeError, match="synthetic DB"):
            await engine.start(caller, customer, "plisio", 100_000, idempotency_key=KEY)
        await caller.rollback()
    async with maker() as observer:
        rows = list(await observer.scalars(select(Payment).where(Payment.customer_id == cid)))
        assert len(rows) == (1 if failed_commit == 2 else 0)
    if failed_commit == 1:
        remote.assert_not_awaited()
    else:
        monkeypatch.setattr("paurubot.payments.durable_checkout.get_sessionmaker", lambda: maker)
        async with maker() as caller:
            customer = await caller.get(Customer, cid)
            with pytest.raises(PaymentError, match="gateway_error"):
                await engine.start(caller, customer, "plisio", 100_000, idempotency_key=KEY)
        remote.assert_awaited_once()


@pytest.mark.parametrize("name", ["nowpayments", "plisio"])
async def test_creation_consumes_the_frozen_conversion(hosted, monkeypatch, name):  # type: ignore[no-untyped-def]
    maker, cid = hosted
    conversion = AsyncMock(side_effect=AssertionError("must not requote after durable commit"))
    monkeypatch.setattr("paurubot.payments.providers.invoices.invoice_cents", conversion)
    provider = engine.provider(name)

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        assert await provider._cents(payment, cfg) == 200
        return checkout()

    monkeypatch.setattr(provider, "create", AsyncMock(side_effect=create))
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        await engine.start(caller, customer, name, 100_000, idempotency_key=KEY)
    conversion.assert_not_awaited()


async def test_legacy_invoice_creation_does_not_reuse_unmarked_amount(monkeypatch):  # type: ignore[no-untyped-def]
    quote = AsyncMock(return_value=345)
    monkeypatch.setattr("paurubot.payments.providers.invoices.invoice_cents", quote)
    payment = Payment(amount=100_000, provider_amount=123, provider_currency="USD", meta={})
    assert await engine.provider("nowpayments")._cents(payment, {}) == 345
    quote.assert_awaited_once_with(payment, {})


@pytest.mark.parametrize("quote_adjustment", [0, 37])
@pytest.mark.parametrize("difference", [-1, 0, 1])
async def test_tonpay_actual_engine_settlement_and_repeated_callback(
    hosted, monkeypatch, difference, quote_adjustment
):  # type: ignore[no-untyped-def]
    import httpx

    from paurubot.db.models import Job
    from paurubot.ledger.accounts import wallet_balance

    maker, cid = hosted
    calls = []
    order_id = ""
    quoted = 100_000 + quote_adjustment

    def handle(request):  # type: ignore[no-untyped-def]
        import json

        nonlocal order_id
        calls.append(request.method)
        if request.method == "POST":
            order_id = json.loads(request.content)["order_id"]
            return httpx.Response(
                200,
                json={
                    "invoice_id": "synthetic-ref",
                    "invoice_url": "https://tonpays.online/pay/test",
                    "order_id": order_id,
                    "request_amount": 100_000,
                    "final_amount": quoted,
                },
            )
        return httpx.Response(
            200,
            json={
                "order_id": order_id,
                "invoice_id": "synthetic-ref",
                "status": "completed",
                "paid": True,
                "request_amount": 100_000,
                "final_amount": quoted + difference,
            },
        )

    monkeypatch.setattr(engine.provider("tonpay"), "_transport", httpx.MockTransport(handle))
    monkeypatch.setattr(engine.provider("tonpay"), "public_url", lambda: "https://shop.invalid")
    async with maker() as session:
        customer = await session.get(Customer, cid)
        await shop_settings.set_many(session, customer.shop_id, {"tonpay_api_key": "synthetic-key"})
        await session.commit()
    shop_settings.invalidate()
    async with maker() as caller:
        customer = await caller.get(Customer, cid)
        started = await engine.start(caller, customer, "tonpay", 100_000, idempotency_key=KEY)
        pid, shop_id = started.payment.id, customer.shop_id
        assert started.checkout.provider_amount == quoted
        await caller.rollback()
    async with maker() as session:
        # The provider's final quote must survive the request rollback/private commit.
        payment = await session.get(Payment, pid)
        assert payment.provider_amount == quoted and payment.provider_currency == "IRT"
        assert await engine.verify(session, pid, {"source": "poll"}) is (difference == 0)
        await session.commit()
    async with maker() as session:
        payment = await session.get(Payment, pid)
        # A changed quote stays uncredited; callback values never override the frozen quote.
        assert payment.status == (PaymentStatus.PAID if difference == 0 else PaymentStatus.PENDING)
        assert payment.provider_tx_id == ("tonpay:synthetic-ref" if difference == 0 else None)
        assert await engine.verify(session, pid, {"json": {"paid": True}}) is (difference == 0)
        await session.commit()
        assert await session.scalar(
            select(func.count(Job.id)).where(
                Job.kind == "fulfill", Job.payload["payment_id"].as_integer() == pid
            )
        ) == (1 if difference == 0 else 0)
    if difference == 0:
        async with maker() as session:
            assert await engine.fulfill_one(session, pid) is not None
        async with maker() as session:
            assert await engine.fulfill_one(session, pid) is None
    async with maker() as session:
        assert await wallet_balance(session, shop_id, cid) == (100_000 if difference == 0 else 0)
    assert calls == (["POST", "GET"] if difference == 0 else ["POST", "GET", "GET"])
