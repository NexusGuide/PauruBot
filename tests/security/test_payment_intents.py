"""Top-up retries have one scoped invoice, not merely idempotent fulfillment."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus, Shop
from paurubot.payments import engine
from paurubot.payments.base import PaymentError
from tests import factories
from tests.conftest import _migrate

pytestmark = pytest.mark.security
CARDS = [{"number": "6037990000000000", "holder": "Test"}]
KEY = "same-client-intent-1"


async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS, "card_random_tail": True})
    customer = await factories.customer(session, shop)
    return shop, customer


async def test_retries_resume_checkout_without_new_provider_request(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, customer = await env(session)
    provider = engine.provider("card")
    create = AsyncMock(wraps=provider.create)
    monkeypatch.setattr(provider, "create", create)
    first = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    await session.commit()
    # Card rotation/config changes must not change the instructions for this invoice.
    await shop_settings.set_many(session, shop.id, {"card_accounts": [{"number": "9999999999999999"}]})
    second = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    assert first.payment.id == second.payment.id
    assert first.payment.amount == second.payment.amount
    assert first.checkout == second.checkout
    assert second.checkout.instructions["accounts"][0]["number"] == CARDS[0]["number"]
    create.assert_awaited_once()


@pytest.mark.parametrize("change", ["amount", "method"])
async def test_same_key_changed_payload_is_conflict(session, change) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    with pytest.raises(PaymentError, match="payment_intent_conflict"):
        await engine.start(
            session,
            customer,
            "stars" if change == "method" else "card",
            200_000 if change == "amount" else 100_000,
            idempotency_key=KEY,
        )


@pytest.mark.parametrize(
    "status", [PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.DISPUTED, PaymentStatus.REFUNDED]
)
async def test_terminal_intent_never_silently_creates_new_invoice(session, status) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    started = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    started.payment.status = status
    await session.flush()
    with pytest.raises(PaymentError, match="payment_intent_closed"):
        await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)


@pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.VERIFYING, PaymentStatus.PAID])
async def test_open_and_paid_intent_replays_same_payment(session, status) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    started = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    started.payment.status = status
    await session.flush()
    again = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    assert again.payment.id == started.payment.id


async def test_expired_pending_intent_is_closed(session) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    started = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    started.payment.expires_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    with pytest.raises(PaymentError, match="payment_intent_closed"):
        await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)


async def test_scope_and_deliberate_new_intents_are_independent(session) -> None:  # type: ignore[no-untyped-def]
    shop, customer = await env(session)
    other_customer = await factories.customer(session, shop)
    _, other_shop_customer = await env(session)
    results = [
        await engine.start(session, c, "card", 100_000, idempotency_key=k)
        for c, k in [
            (customer, KEY),
            (other_customer, KEY),
            (other_shop_customer, KEY),
            (customer, "another-intent-2"),
        ]
    ]
    assert len({p.payment.id for p in results}) == 4


async def test_legacy_unkeyed_creation_still_allows_new_topups(session) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    a = await engine.start(session, customer, "card", 100_000)
    b = await engine.start(session, customer, "card", 100_000)
    assert a.payment.id != b.payment.id


@pytest.mark.parametrize("key", ["", "short", "x" * 129, "wrong key value", "کلیدنامعتبر"])
async def test_invalid_key_rejected_before_creation(session, key) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    with pytest.raises(PaymentError, match="payment_intent_invalid"):
        await engine.start(session, customer, "card", 100_000, idempotency_key=key)
    assert await session.scalar(select(func.count(Payment.id))) == 0


async def test_corrupt_checkout_snapshot_fails_closed(session) -> None:  # type: ignore[no-untyped-def]
    _, customer = await env(session)
    started = await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)
    started.payment.meta = {}
    await session.flush()
    with pytest.raises(PaymentError, match="payment_intent_conflict"):
        await engine.start(session, customer, "card", 100_000, idempotency_key=KEY)


@pytest.fixture(scope="module")
async def intent_engine(engine):  # type: ignore[no-untyped-def]
    schema = f"test_intents_{uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_async_engine(engine.url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with isolated.begin() as conn:
            await conn.run_sync(_migrate)
        shop_settings.invalidate()
        yield isolated
    finally:
        await isolated.dispose()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        shop_settings.invalidate()


@pytest.mark.parametrize("different_amount", [False, True])
async def test_real_concurrent_commits_create_one_invoice(
    intent_engine, monkeypatch, different_amount
) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    async with maker() as session:
        shop = Shop(title="Isolated intents")
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
        customer = await factories.customer(session, shop)
        cid = customer.id
        await session.commit()
    prov = engine.provider("card")
    create = AsyncMock(wraps=prov.create)
    monkeypatch.setattr(prov, "create", create)
    barrier = asyncio.Barrier(4)

    async def request(i):  # type: ignore[no-untyped-def]
        async with maker() as session:
            customer = await session.get(Customer, cid)
            await barrier.wait()
            try:
                result = await engine.start(
                    session,
                    customer,
                    "card",
                    100_000 + (i * 1000 if different_amount else 0),
                    idempotency_key=KEY,
                )
                await session.commit()
                return result.payment.id
            except PaymentError as exc:
                await session.rollback()
                return exc.key

    outcomes = await asyncio.wait_for(asyncio.gather(*(request(i) for i in range(4))), timeout=20)
    assert len({x for x in outcomes if isinstance(x, int)}) == 1
    assert outcomes.count("payment_intent_conflict") == (3 if different_amount else 0)
    create.assert_awaited_once()
    async with maker() as session:
        assert await session.scalar(select(func.count(Payment.id)).where(Payment.customer_id == cid)) == 1
