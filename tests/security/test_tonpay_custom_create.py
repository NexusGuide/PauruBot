"""Durable single-attempt creation. No registered provider, real key or live network."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus, Shop, ShopStatus
from paurubot.payments import tonpay_custom_create as create
from paurubot.payments import tonpay_custom_service as mutations
from paurubot.payments.providers.tonpay_custom import CustomAPIError, CustomInvoice, TonpayCustomClient
from tests import factories
from tests.security.test_payment_intents import intent_engine as intent_engine

pytestmark = pytest.mark.security
KEY = "custom-create-test-0001"
AMOUNT = 100_000


@pytest.fixture
async def creation(intent_engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    monkeypatch.setattr(create, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(mutations, "get_sessionmaker", lambda: maker)
    async with maker() as session:
        shop = Shop(title="Unwired creation test")
        session.add(shop)
        await session.flush()
        customer = await factories.customer(session, shop)
        owner = create.Owner(shop.id, customer.id)
        await session.commit()
    policy = create.CreationPolicy(10_000, 1_000_000, 86400, 600, 60, 3600)
    cfg = create.CreationConfig(
        owner.shop_id, "synthetic-key-epoch-01", "https://example.com/callback", policy, True
    )
    client = TonpayCustomClient("synthetic-key")

    async def response(order_id, amount, buyer_chat_id, callback_url):  # type: ignore[no-untyped-def]
        return CustomInvoice("invoice_" + order_id, order_id, amount, amount + 3000, "pending", False)

    monkeypatch.setattr(client, "create", AsyncMock(side_effect=response))
    # intent_engine is module-scoped in CI: previous cases legitimately committed
    # rows in other shops. Assertions below must inspect this case, not that history.
    maker.configure(info={"custom_case_shop_id": owner.shop_id})
    return maker, owner, cfg, client


async def start(creation, key=KEY, amount=AMOUNT):  # type: ignore[no-untyped-def]
    _, owner, cfg, client = creation
    return await create.create_once(owner, amount, key, config=cfg, client=client)


def _payments(session):  # type: ignore[no-untyped-def]
    return select(Payment).where(Payment.shop_id == session.info["custom_case_shop_id"])


async def only_payment(maker):  # type: ignore[no-untyped-def]
    async with maker() as session:
        return (await session.scalars(_payments(session))).one()


async def test_committed_marker_and_principal_precede_http(creation):  # type: ignore[no-untyped-def]
    maker, owner, cfg, client = creation

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        async with maker() as independent:
            payment = await independent.scalar(_payments(independent))
            customer = await independent.get(Customer, owner.customer_id)
            assert payment.id == int(order_id)
            assert buyer == customer.telegram_id and callback == cfg.callback_url
            assert payment.status == PaymentStatus.CREATED and payment.provider_ref is None
            assert payment.meta[create.CREATE_KEY]["state"] == "uncertain"
            assert create.STATE_KEY not in payment.meta
        return CustomInvoice("bound_invoice", order_id, amount, 103_000, "pending", False)

    client.create.side_effect = response
    result = await start(creation)
    assert result == create.CreatedQuote(result.payment_id, 1, AMOUNT, 103_000, False)
    payment = await only_payment(maker)
    assert payment.status == PaymentStatus.PENDING and payment.amount == AMOUNT
    state = mutations._state(payment)
    assert state["consent_revision"] is None and state["operations"] == {}
    assert state["policy"] == {"receipt_wait": 600, "card_cooldown": 60, "review_window": 3600}
    text = str(payment.meta)
    assert "synthetic-key'" not in text and "https://example.com" not in text
    assert "card_number" not in text and "card_name" not in text
    assert client.create.await_count == 1


async def test_same_key_replays_current_quote_without_creation_or_consent(creation):  # type: ignore[no-untyped-def]
    maker, owner, _, client = creation
    first = await start(creation)
    assert await start(creation) == first
    async with maker() as session:
        payment = await session.get(Payment, first.payment_id)
        state = deepcopy(payment.meta[create.STATE_KEY])
        state["quote"].update(revision=2, payable_amount=105_000)
        payment.meta = {**payment.meta, create.STATE_KEY: state}
        await session.commit()
    assert await start(creation) == create.CreatedQuote(first.payment_id, 2, AMOUNT, 105_000, False)
    await mutations.consent(owner, first.payment_id, 2, 105_000)
    client.create.assert_awaited_once()


@pytest.mark.parametrize("change", ["amount", "epoch", "callback", "policy"])
async def test_changed_fingerprint_conflicts(creation, change):  # type: ignore[no-untyped-def]
    _, owner, cfg, client = creation
    await start(creation)
    amount = AMOUNT
    if change == "amount":
        amount += 1
    elif change == "epoch":
        cfg = replace(cfg, credential_epoch="synthetic-key-epoch-02")
    elif change == "callback":
        cfg = replace(cfg, callback_url="https://example.com/other")
    else:
        cfg = replace(cfg, policy=replace(cfg.policy, receipt_wait=700))
    with pytest.raises(create.CustomStateError) as exc:
        await create.create_once(owner, amount, KEY, config=cfg, client=client)
    assert exc.value.code == "intent_conflict"
    client.create.assert_awaited_once()


@pytest.mark.parametrize("failure", [CustomAPIError("upstream_error"), asyncio.CancelledError()])
async def test_ambiguous_failure_blocks_same_and_replacement_keys_even_after_expiry(creation, failure):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation
    client.create.side_effect = failure
    with pytest.raises(type(failure)):
        await start(creation)
    async with maker() as session:
        payment = await session.scalar(_payments(session))
        payment.status = PaymentStatus.EXPIRED
        payment.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    for key in (KEY, KEY + "-replacement"):
        with pytest.raises(create.CustomStateError) as exc:
            await start(creation, key)
        assert exc.value.code == "creation_uncertain"
    client.create.assert_awaited_once()


@pytest.mark.parametrize(
    "field",
    ["enabled", "shop", "epoch", "callback", "minimum", "ttl", "wait", "bool_amount", "float_amount", "key"],
)
async def test_invalid_or_disabled_config_never_writes_or_calls(creation, field):  # type: ignore[no-untyped-def]
    maker, owner, cfg, client = creation
    amount, key = AMOUNT, KEY
    if field == "enabled":
        cfg = replace(cfg, enabled=False)
    elif field == "shop":
        cfg = replace(cfg, shop_id=owner.shop_id + 1)
    elif field == "epoch":
        cfg = replace(cfg, credential_epoch="secret?")
    elif field == "callback":
        cfg = replace(cfg, callback_url="http://127.0.0.1")
    elif field == "minimum":
        cfg = replace(cfg, policy=replace(cfg.policy, minimum_amount=AMOUNT + 1))
    elif field == "ttl":
        cfg = replace(cfg, policy=replace(cfg.policy, invoice_ttl=600))
    elif field == "wait":
        cfg = replace(cfg, policy=replace(cfg.policy, receipt_wait=True))
    elif field == "bool_amount":
        amount = True
    elif field == "float_amount":
        amount = float(AMOUNT)
    else:
        key = "short"
    with pytest.raises((create.CustomStateError, CustomAPIError)):
        await create.create_once(owner, amount, key, config=cfg, client=client)
    async with maker() as session:
        assert await session.scalar(_payments(session).with_only_columns(Payment.id)) is None
    client.create.assert_not_awaited()


@pytest.mark.parametrize("case", ["banned", "guest", "inactive", "wrong_shop", "missing"])
async def test_unavailable_principals_fail_before_marker_or_http(creation, case):  # type: ignore[no-untyped-def]
    maker, owner, cfg, client = creation
    async with maker() as session:
        customer = await session.get(Customer, owner.customer_id)
        if case == "banned":
            customer.is_banned = True
        elif case == "guest":
            customer.telegram_id = -1
        elif case == "inactive":
            shop = await session.get(Shop, owner.shop_id)
            shop.status = ShopStatus.SUSPENDED
        elif case == "wrong_shop":
            other = Shop(title="Other")
            session.add(other)
            await session.flush()
            owner = create.Owner(other.id, owner.customer_id)
            cfg = replace(cfg, shop_id=other.id)
        else:
            owner = create.Owner(owner.shop_id, owner.customer_id + 1000)
        await session.commit()
    with pytest.raises(create.CustomStateError):
        await create.create_once(owner, AMOUNT, KEY, config=cfg, client=client)
    client.create.assert_not_awaited()
    async with maker() as session:
        assert await session.scalar(_payments(session).with_only_columns(Payment.id)) is None


async def test_initial_commit_failure_never_calls_provider(creation, monkeypatch):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation

    async def fail(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail)
    with pytest.raises(RuntimeError):
        await start(creation)
    client.create.assert_not_awaited()
    async with maker() as session:
        assert await session.scalar(_payments(session).with_only_columns(Payment.id)) is None


async def test_final_commit_failure_keeps_uncertain_marker(creation, monkeypatch):  # type: ignore[no-untyped-def]
    original = AsyncSession.commit
    count = 0

    async def fail_second(self):  # type: ignore[no-untyped-def]
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic final commit failure")
        return await original(self)

    monkeypatch.setattr(AsyncSession, "commit", fail_second)
    with pytest.raises(RuntimeError):
        await start(creation)
    maker, _, _, client = creation
    payment = await only_payment(maker)
    assert payment.meta[create.CREATE_KEY]["state"] == "uncertain" and payment.provider_ref is None
    for key in (KEY, KEY + "-new"):
        with pytest.raises(create.CustomStateError):
            await start(creation, key)
    client.create.assert_awaited_once()


@pytest.mark.parametrize("change", ["closed", "expired", "ban", "amount", "token"])
async def test_inflight_changes_never_resurrect_or_credit(creation, change):  # type: ignore[no-untyped-def]
    maker, owner, _, client = creation

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        async with maker() as session:
            payment = await session.get(Payment, int(order_id))
            payment.meta = {**payment.meta, "unrelated": "preserved"}
            if change == "closed":
                payment.status = PaymentStatus.FAILED
            elif change == "expired":
                payment.expires_at = utcnow() - timedelta(seconds=1)
            elif change == "ban":
                customer = await session.get(Customer, owner.customer_id)
                customer.is_banned = True
            elif change == "amount":
                payment.amount += 1
            else:
                attempt = {**payment.meta[create.CREATE_KEY], "token": "a" * 32}
                payment.meta = {**payment.meta, create.CREATE_KEY: attempt}
            await session.commit()
        return CustomInvoice("invoice_" + order_id, order_id, amount, 103_000, "pending", False)

    client.create.side_effect = response
    if change in {"closed", "expired"}:
        result = await start(creation)
        assert result.needs_review
    else:
        with pytest.raises(create.CustomStateError):
            await start(creation)
    payment = await only_payment(maker)
    assert payment.status != PaymentStatus.PAID
    assert payment.meta["unrelated"] == "preserved"
    if change == "closed":
        assert payment.status == PaymentStatus.FAILED
    elif change == "expired":
        assert payment.status == PaymentStatus.EXPIRED
    else:
        assert payment.provider_ref is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("invoice_id", "bad/reference"),
        ("order_id", "999999"),
        ("request_amount", 999),
        ("request_amount", 100000.0),
        ("payable_amount", True),
        ("payable_amount", 99000),
        ("paid", "false"),
        ("status", 4),
    ],
)
async def test_invalid_response_keeps_unbound_uncertain_attempt(creation, field, value):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        return replace(
            CustomInvoice("invoice_test", order_id, amount, 103000, "pending", False), **{field: value}
        )

    client.create.side_effect = response
    with pytest.raises(create.CustomStateError):
        await start(creation)
    payment = await only_payment(maker)
    assert payment.provider_ref is None and payment.meta[create.CREATE_KEY]["state"] == "uncertain"


async def test_paid_reply_is_observation_not_wallet_credit(creation):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        return CustomInvoice("paid_invoice", order_id, amount, 103000, "completed", True)

    client.create.side_effect = response
    result = await start(creation)
    assert result.needs_review
    payment = await only_payment(maker)
    assert payment.status == PaymentStatus.PENDING and payment.amount == AMOUNT
    with pytest.raises(create.CustomStateError):
        await mutations.consent(creation[1], payment.id, 1, 103000)


async def test_completed_new_intent_does_not_reuse_provider_reference(creation):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        return CustomInvoice("duplicate_invoice", order_id, amount, 103000, "pending", False)

    client.create.side_effect = response
    await start(creation)
    with pytest.raises(create.CustomStateError):
        await start(creation, KEY + "-new")
    async with maker() as session:
        payments = list(await session.scalars(_payments(session).order_by(Payment.id)))
        assert len(payments) == 2
        assert payments[0].provider_ref == "duplicate_invoice" and payments[1].provider_ref is None
        assert payments[1].meta[create.CREATE_KEY]["state"] == "uncertain"


async def test_inflight_second_key_blocked_before_remote_completion(creation):  # type: ignore[no-untyped-def]
    _, _, _, client = creation
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return CustomInvoice("blocked_invoice", order_id, amount, 103000, "pending", False)

    client.create.side_effect = response
    task = asyncio.create_task(start(creation))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for key in (KEY, KEY + "-different"):
            with pytest.raises(create.CustomStateError) as exc:
                await start(creation, key)
            assert exc.value.code == "creation_uncertain"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    client.create.assert_awaited_once()


async def test_real_concurrent_claims_commit_once_for_different_keys(creation):  # type: ignore[no-untyped-def]
    """Requires PostgreSQL customer FOR UPDATE; SQLite is not evidence for this race."""
    maker, _, _, client = creation
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return CustomInvoice("concurrent_invoice", order_id, amount, 103000, "pending", False)

    client.create.side_effect = response
    tasks = [asyncio.create_task(start(creation, KEY + str(i))) for i in range(4)]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # All other tasks must finish by observing the committed uncertain row, not reach HTTP.
        for _ in range(200):
            if sum(t.done() for t in tasks) == 3:
                break
            await asyncio.sleep(0.01)
        assert sum(t.done() for t in tasks) == 3
        assert client.create.await_count == 1
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
    assert sum(isinstance(r, create.CreatedQuote) for r in results) == 1
    assert (
        sum(isinstance(r, create.CustomStateError) and r.code == "creation_uncertain" for r in results) == 3
    )
    async with maker() as session:
        assert len(list(await session.scalars(_payments(session).with_only_columns(Payment.id)))) == 1


async def test_real_client_wire_creation_binds_only_its_checked_response(creation):  # type: ignore[no-untyped-def]
    maker, owner, cfg, _ = creation
    calls = []

    async def transport(request):  # type: ignore[no-untyped-def]
        import json

        data = json.loads(request.content)
        calls.append(data)
        assert str(request.url) == "https://tonpays.online/api/custom/v1/invoices/telegram/create"
        assert request.headers["X-API-Key"] == "synthetic-wire-key"
        payment = await only_payment(maker)
        assert data["order_id"] == str(payment.id)
        async with maker() as session:
            customer = await session.get(Customer, owner.customer_id)
            assert data["buyer_chat_id"] == customer.telegram_id
        assert data["amount"] == AMOUNT and data["callback_url"] == cfg.callback_url
        return httpx.Response(
            200,
            json={
                "invoice_id": "wire_invoice",
                "order_id": data["order_id"],
                "request_amount": AMOUNT,
                "final_amount": 103000,
                "paid": False,
                "status": "pending",
                "card_number": "0000000000000000",
                "card_name": "Synthetic only",
            },
        )

    client = TonpayCustomClient("synthetic-wire-key", transport=httpx.MockTransport(transport))
    result = await create.create_once(owner, AMOUNT, KEY, config=cfg, client=client)
    assert await create.create_once(owner, AMOUNT, KEY, config=cfg, client=client) == result
    assert len(calls) == 1
    payment = await only_payment(maker)
    assert payment.provider_ref == "wire_invoice"
    assert "0000000000000000" not in str(payment.meta) and "Synthetic only" not in str(payment.meta)


async def test_private_commit_does_not_commit_unrelated_callers_writes(creation):  # type: ignore[no-untyped-def]
    maker, owner, _, _ = creation
    async with maker() as caller:
        shop = await caller.get(Shop, owner.shop_id)
        original = shop.title
        shop.title = "Must not be committed by creator"
        # No flush: an unrelated write is owned by this caller, never passed to creator.
        await start(creation)
        await caller.rollback()
    async with maker() as session:
        assert (await session.get(Shop, owner.shop_id)).title == original
        assert await session.scalar(_payments(session).with_only_columns(Payment.id)) is not None


async def test_ready_but_closed_replay_never_creates_another_invoice(creation):  # type: ignore[no-untyped-def]
    maker, _, _, client = creation
    result = await start(creation)
    async with maker() as session:
        payment = await session.get(Payment, result.payment_id)
        payment.status = PaymentStatus.FAILED
        await session.commit()
    with pytest.raises(create.CustomStateError) as exc:
        await start(creation)
    assert exc.value.code == "payment_closed"
    client.create.assert_awaited_once()


@pytest.mark.parametrize("phase", ["before_claim", "before_bind"])
async def test_blocked_card_trust_prevents_claim_or_binding(creation, phase):  # type: ignore[no-untyped-def]
    maker, owner, _, client = creation

    async def block():  # type: ignore[no-untyped-def]
        async with maker() as session:
            customer = await session.get(Customer, owner.customer_id)
            customer.card_trust = "blocked"
            await session.commit()

    if phase == "before_claim":
        await block()
    else:

        async def response(order_id, amount, buyer, callback):  # type: ignore[no-untyped-def]
            await block()
            return CustomInvoice("risk_changed_invoice", order_id, amount, amount + 3000, "pending", False)

        client.create.side_effect = response
    with pytest.raises(create.CustomStateError):
        await start(creation)
    if phase == "before_claim":
        client.create.assert_not_awaited()
    else:
        p = await only_payment(maker)
        assert p.provider_ref is None and p.meta[create.CREATE_KEY]["state"] == "uncertain"
