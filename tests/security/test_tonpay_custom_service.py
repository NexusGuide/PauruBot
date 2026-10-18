"""Independent commits and HTTP seams for an unregistered custom mutation service."""

from __future__ import annotations

import asyncio
import io
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentPurpose, PaymentStatus, Shop, ShopStatus
from paurubot.payments import tonpay_custom_service as service
from paurubot.payments.providers.tonpay_custom import CustomAPIError, CustomInvoice, TonpayCustomClient
from tests import factories
from tests.security.test_payment_intents import intent_engine as intent_engine

pytestmark = pytest.mark.security
KEY = "custom-mutation-key-0001"


def image_bytes():  # type: ignore[no-untyped-def]
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def bound(intent_engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    monkeypatch.setattr(service, "get_sessionmaker", lambda: maker)
    async with maker() as session:
        shop = Shop(title="Unregistered custom test")
        session.add(shop)
        await session.flush()
        customer = await factories.customer(session, shop)
        payment = Payment(
            shop_id=shop.id,
            customer_id=customer.id,
            provider="tonpay_custom",
            amount=100_000,
            purpose=PaymentPurpose.TOPUP,
            status=PaymentStatus.PENDING,
            currency="IRT",
            idempotency_key=f"custom-seed-{uuid4().hex}",
            provider_ref="invoice_test",
            expires_at=utcnow() + timedelta(days=1),
            meta={"unrelated": "preserve-me"},
        )
        session.add(payment)
        await session.flush()
        payment.meta = {
            **payment.meta,
            service.STATE_KEY: {
                "schema": 1,
                "invoice_id": "invoice_test",
                "quote": {"revision": 1, "request_amount": 100_000, "payable_amount": 103_000},
                # Synthetic explicit policy; no production default or enablement is introduced.
                "policy": {"receipt_wait": 600, "card_cooldown": 60, "review_window": 86400},
                "created_at": service._now() - 601,
                "remote_status": "pending",
                "paid": False,
                "receipt_received": False,
                "change_card_exhausted": False,
                "operations": {},
                "consent_revision": None,
            },
        }
        pid = payment.id
        owner = service.Owner(shop.id, customer.id)
        await session.commit()
    client = TonpayCustomClient("synthetic-key")
    invoice = CustomInvoice(
        "invoice_test", str(pid), 100_000, 103_000, "pending", False, "0000000000000000", "Synthetic"
    )
    monkeypatch.setattr(client, "change_card", AsyncMock(return_value=invoice))
    ack = CustomInvoice(
        "invoice_test", str(pid), 100_000, 103_000, "processing", False, receipt_received=True
    )
    monkeypatch.setattr(client, "submit_receipt", AsyncMock(return_value=ack))
    return maker, owner, pid, client


async def update_state(bound, change):  # type: ignore[no-untyped-def]
    maker, _, pid, _ = bound
    async with maker() as session:
        payment = await session.get(Payment, pid)
        state = deepcopy(payment.meta[service.STATE_KEY])
        change(state)
        payment.meta = {**payment.meta, service.STATE_KEY: state}
        # Force a JSON write even when Python considers 1 and 1.0 equal.
        flag_modified(payment, "meta")
        await session.commit()


async def test_receipt_requires_exact_current_quote_consent(bound):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    assert exc.value.code == "consent_required"
    client.submit_receipt.assert_not_awaited()
    with pytest.raises(service.CustomStateError) as exc:
        await service.consent(owner, pid, 1, 100_000)
    assert exc.value.code == "stale_quote"
    await service.consent(owner, pid, 1, 103_000)
    first = await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    again = await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    assert first == again and first.receipt_received and not first.needs_review
    client.submit_receipt.assert_awaited_once_with("invoice_test", str(pid), 100_000, image_bytes())


@pytest.mark.parametrize("wrong", ["customer", "shop", "banned", "guest", "shop_inactive"])
async def test_owner_scope_denied_before_image_validation_or_network(bound, monkeypatch, wrong):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    if wrong == "customer":
        owner = service.Owner(owner.shop_id, owner.customer_id + 100_000)
    elif wrong == "shop":
        owner = service.Owner(owner.shop_id + 100_000, owner.customer_id)
    else:
        async with maker() as session:
            customer = await session.get(Customer, owner.customer_id)
            if wrong == "banned":
                customer.is_banned = True
            elif wrong == "guest":
                customer.telegram_id = -42
            else:
                shop = await session.get(Shop, owner.shop_id)
                # A non-active persisted value, not a request-supplied scope claim.
                shop.status = next(value for value in ShopStatus if value != ShopStatus.ACTIVE)
            await session.commit()

    def must_not_decode(content):  # type: ignore[no-untyped-def]
        raise AssertionError("unauthorized caller reached image decoding")

    monkeypatch.setattr(service, "receipt_image", must_not_decode)
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, KEY, 1, b"invalid", client=client)
    assert exc.value.code == "not_found"
    client.submit_receipt.assert_not_awaited()
    client.change_card.assert_not_awaited()


@pytest.mark.parametrize(
    "field", ["receipt_not_ready", "review_expired", "stale_quote", "remote_processing", "already_paid"]
)
async def test_receipt_time_state_and_quote_gates(bound, field):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)

    def change(state):  # type: ignore[no-untyped-def]
        if field == "receipt_not_ready":
            state["created_at"] = service._now()
        elif field == "review_expired":
            state["review_until"] = service._now() - 1
        elif field == "stale_quote":
            state["quote"]["revision"] = 2
        elif field == "remote_processing":
            state["remote_status"] = "processing"
        else:
            state["paid"] = True

    await update_state(bound, change)
    with pytest.raises(service.CustomStateError):
        await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    client.submit_receipt.assert_not_awaited()


async def test_claim_committed_and_row_lock_released_before_remote_io(bound, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound

    async def remote(*args):  # type: ignore[no-untyped-def]
        async with maker() as observer:
            payment = await observer.scalar(
                select(Payment).where(Payment.id == pid).with_for_update(nowait=True)
            )
            assert payment is not None
            assert next(iter(payment.meta[service.STATE_KEY]["operations"].values()))["state"] == "uncertain"
        return CustomInvoice("invoice_test", str(pid), 100_000, 104_000, "pending", False, "0000000000000000")

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))
    result = await service.change_card(owner, pid, KEY, 1, client=client)
    assert result.payable_amount == 104_000 and result.quote_revision == 2 and result.needs_review
    async with maker() as observer:
        payment = await observer.get(Payment, pid)
        assert payment.status == PaymentStatus.PENDING
        assert payment.meta["unrelated"] == "preserve-me"
        assert payment.meta[service.STATE_KEY]["consent_revision"] is None
        assert "card_number" not in str(payment.meta) and "Synthetic" not in str(payment.meta)


async def test_card_change_invalidates_consent_even_for_same_amount_and_enforces_cooldown(bound):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)
    result = await service.change_card(owner, pid, KEY, 1, client=client)
    assert result.quote_revision == 2
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, "different-intent-0002", 2, image_bytes(), client=client)
    assert exc.value.code == "consent_required"
    with pytest.raises(service.CustomStateError) as exc:
        await service.change_card(owner, pid, "different-intent-0003", 2, client=client)
    assert exc.value.code == "card_change_unavailable"
    assert (await service.change_card(owner, pid, KEY, 1, client=client)) == result
    client.change_card.assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [CustomAPIError("upstream_error"), TimeoutError(), asyncio.CancelledError()]
)
async def test_uncertain_attempt_blocks_same_new_and_other_operation_keys(bound, monkeypatch, failure):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await service.change_card(owner, pid, KEY, 1, client=client)
    for key in (KEY, "replacement-intent-0002"):
        with pytest.raises(service.CustomStateError) as exc:
            await service.change_card(owner, pid, key, 1, client=client)
        assert exc.value.code == "mutation_uncertain"
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, "replacement-receipt-003", 1, image_bytes(), client=client)
    assert exc.value.code == "mutation_uncertain"
    client.change_card.assert_awaited_once()
    client.submit_receipt.assert_not_awaited()


async def test_inflight_duplicate_waits_only_for_commit_not_remote_call(bound, monkeypatch):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    entered, release = asyncio.Event(), asyncio.Event()

    async def remote(*args):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return CustomInvoice("invoice_test", str(pid), 100_000, 103_000, "pending", False)

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))
    task = asyncio.create_task(service.change_card(owner, pid, KEY, 1, client=client))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(service.CustomStateError) as exc:
            await asyncio.wait_for(service.change_card(owner, pid, KEY, 1, client=client), 5)
        assert exc.value.code == "mutation_uncertain"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    client.change_card.assert_awaited_once()


async def test_first_commit_failure_prevents_external_call(bound, monkeypatch):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(
        AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("synthetic commit failure"))
    )
    with pytest.raises(RuntimeError):
        await service.change_card(owner, pid, KEY, 1, client=client)
    client.change_card.assert_not_awaited()


async def test_post_remote_commit_failure_remains_uncertain(bound, monkeypatch):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    from sqlalchemy.ext.asyncio import AsyncSession

    original = AsyncSession.commit
    count = 0

    async def commit(session):  # type: ignore[no-untyped-def]
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic post-remote commit failure")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with pytest.raises(RuntimeError):
        await service.change_card(owner, pid, KEY, 1, client=client)
    monkeypatch.setattr(AsyncSession, "commit", original)
    with pytest.raises(service.CustomStateError) as exc:
        await service.change_card(owner, pid, KEY, 1, client=client)
    assert exc.value.code == "mutation_uncertain"
    client.change_card.assert_awaited_once()


@pytest.mark.parametrize("closed_by", ["status", "expiry"])
async def test_concurrent_rejection_is_not_resurrected(bound, monkeypatch, closed_by):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound

    async def remote(*args):  # type: ignore[no-untyped-def]
        async with maker() as reviewer:
            payment = await reviewer.get(Payment, pid)
            if closed_by == "status":
                payment.status = PaymentStatus.FAILED
            else:
                payment.expires_at = utcnow() - timedelta(seconds=1)
            payment.meta = {**payment.meta, "staff_evidence": "keep"}
            await reviewer.commit()
        return CustomInvoice("invoice_test", str(pid), 100_000, 103_000, "pending", False)

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))
    assert (await service.change_card(owner, pid, KEY, 1, client=client)).needs_review
    async with maker() as observer:
        payment = await observer.get(Payment, pid)
        assert payment.meta["staff_evidence"] == "keep"
        if closed_by == "status":
            assert payment.status == PaymentStatus.FAILED
        else:
            assert payment.status == PaymentStatus.PENDING and payment.expires_at < utcnow()


async def test_invalid_image_does_not_poison_intent(bound):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    with pytest.raises(CustomAPIError):
        await service.submit_receipt(owner, pid, KEY, 1, b"not an image", client=client)
    async with maker() as session:
        assert not (await session.get(Payment, pid)).meta[service.STATE_KEY]["operations"]
    client.submit_receipt.assert_not_awaited()


async def test_same_key_different_operation_or_revision_conflicts(bound):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    await service.change_card(owner, pid, KEY, 1, client=client)
    with pytest.raises(service.CustomStateError) as exc:
        await service.change_card(owner, pid, KEY, 2, client=client)
    assert exc.value.code == "intent_conflict"
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    assert exc.value.code == "intent_conflict"
    client.change_card.assert_awaited_once()


@pytest.mark.parametrize("field", ["provider", "purpose", "currency", "expired", "closed", "no_deadline"])
async def test_wrong_provider_currency_purpose_or_closed_payment_never_calls_provider(bound, field):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    async with maker() as session:
        payment = await session.get(Payment, pid)
        if field == "provider":
            payment.provider = "card"
        elif field == "purpose":
            payment.purpose = PaymentPurpose.ORDER
        elif field == "currency":
            payment.currency = "USD"
        elif field == "expired":
            payment.expires_at = utcnow() - timedelta(seconds=1)
        elif field == "closed":
            payment.status = PaymentStatus.PAID
        else:
            payment.expires_at = None
        await session.commit()
    with pytest.raises(service.CustomStateError):
        await service.change_card(owner, pid, KEY, 1, client=client)
    client.change_card.assert_not_awaited()


async def test_changed_receipt_bytes_with_same_key_is_conflict(bound):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)
    await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, KEY, 1, buffer.getvalue(), client=client)
    assert exc.value.code == "intent_conflict"
    client.submit_receipt.assert_awaited_once()


async def test_ban_during_image_validation_is_rechecked_before_claim(bound, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)
    original = service.receipt_image
    loop = asyncio.get_running_loop()

    async def ban():
        async with maker() as session:
            customer = await session.get(Customer, owner.customer_id)
            customer.is_banned = True
            await session.commit()

    def validate(body):  # type: ignore[no-untyped-def]
        asyncio.run_coroutine_threadsafe(ban(), loop).result(timeout=5)
        return original(body)

    monkeypatch.setattr(service, "receipt_image", validate)
    with pytest.raises(service.CustomStateError) as exc:
        await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    assert exc.value.code == "not_found"
    client.submit_receipt.assert_not_awaited()


async def test_paid_upload_acknowledgement_does_not_settle_payment(bound, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)
    monkeypatch.setattr(
        client,
        "submit_receipt",
        AsyncMock(
            return_value=CustomInvoice(
                "invoice_test",
                str(pid),
                100_000,
                104_000,
                "completed",
                True,
                receipt_received=True,
            )
        ),
    )
    result = await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    assert result.needs_review and result.payable_amount == 104_000
    async with maker() as session:
        payment = await session.get(Payment, pid)
        assert payment.status == PaymentStatus.PENDING and payment.amount == 100_000
        assert payment.meta[service.STATE_KEY]["consent_revision"] is None


async def test_evidence_history_is_bounded_without_eviction(bound):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound

    def fill(state):  # type: ignore[no-untyped-def]
        state["operations"] = {
            f"{i:064x}": {"state": "done", "token": f"{i:032x}", "fingerprint": {}}
            for i in range(service.MAX_OPERATIONS)
        }

    await update_state(bound, fill)
    with pytest.raises(service.CustomStateError) as exc:
        await service.change_card(owner, pid, KEY, 1, client=client)
    assert exc.value.code == "history_full"
    client.change_card.assert_not_awaited()


@pytest.mark.parametrize("field", ["policy", "schema", "reference", "quote"])
async def test_malformed_local_state_fails_closed(bound, field):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound

    def corrupt(state):  # type: ignore[no-untyped-def]
        if field == "policy":
            del state["policy"]["receipt_wait"]
        elif field == "schema":
            state["schema"] = 1.0
        elif field == "reference":
            state["invoice_id"] = "other"
        else:
            state["quote"]["request_amount"] = 99_999

    await update_state(bound, corrupt)
    with pytest.raises(service.CustomStateError):
        await service.change_card(owner, pid, KEY, 1, client=client)
    client.change_card.assert_not_awaited()


async def test_quote_changed_during_remote_operation_leaves_uncertainty(bound, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound

    async def remote(*args):  # type: ignore[no-untyped-def]
        await update_state(bound, lambda state: state["quote"].update(revision=2))
        return CustomInvoice("invoice_test", str(pid), 100_000, 103_000, "pending", False)

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))
    with pytest.raises(service.CustomStateError) as exc:
        await service.change_card(owner, pid, KEY, 1, client=client)
    assert exc.value.code == "mutation_uncertain"
    async with maker() as session:
        state = (await session.get(Payment, pid)).meta[service.STATE_KEY]
        assert next(iter(state["operations"].values()))["state"] == "uncertain"


async def test_real_concurrent_claims_send_one_remote_mutation(bound, monkeypatch):  # type: ignore[no-untyped-def]
    _, owner, pid, client = bound
    barrier = asyncio.Barrier(4)
    entered, release = asyncio.Event(), asyncio.Event()
    results = asyncio.Queue()

    async def remote(*args):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return CustomInvoice("invoice_test", str(pid), 100_000, 103_000, "pending", False)

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))

    async def request():
        await barrier.wait()
        try:
            result = await service.change_card(owner, pid, KEY, 1, client=client)
        except service.CustomStateError as exc:
            await results.put(exc.code)
        else:
            await results.put(result)

    tasks = [asyncio.create_task(request()) for _ in range(4)]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        denied = [await asyncio.wait_for(results.get(), 5) for _ in range(3)]
        assert denied == ["mutation_uncertain"] * 3
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert isinstance(await results.get(), service.MutationResult)
    client.change_card.assert_awaited_once()


async def test_older_reply_does_not_downgrade_concurrent_paid_observation(bound, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound

    async def remote(*args):  # type: ignore[no-untyped-def]
        await update_state(
            bound,
            lambda state: state.update(paid=True, remote_status="completed", change_card_exhausted=True),
        )
        return CustomInvoice("invoice_test", str(pid), 100_000, 103_000, "pending", False)

    monkeypatch.setattr(client, "change_card", AsyncMock(side_effect=remote))
    result = await service.change_card(owner, pid, KEY, 1, client=client)
    assert result.needs_review
    async with maker() as session:
        payment = await session.get(Payment, pid)
        state = payment.meta[service.STATE_KEY]
        assert state["paid"] and state["remote_status"] == "completed" and state["change_card_exhausted"]
        assert payment.status == PaymentStatus.PENDING


async def test_new_card_can_receive_new_receipt_without_erasing_old_ack_or_extending_review(bound):  # type: ignore[no-untyped-def]
    maker, owner, pid, client = bound
    await service.consent(owner, pid, 1, 103_000)
    await service.submit_receipt(owner, pid, KEY, 1, image_bytes(), client=client)
    async with maker() as session:
        deadline = (await session.get(Payment, pid)).meta[service.STATE_KEY]["review_until"]
    # A future authenticated refresh writes need_action; this is synthetic state only.
    await update_state(bound, lambda state: state.update(remote_status="need_action"))
    await service.change_card(owner, pid, "change-card-intent-0002", 1, client=client)
    await service.consent(owner, pid, 2, 103_000)
    await service.submit_receipt(owner, pid, "receipt-intent-new-0003", 2, image_bytes(), client=client)
    assert client.submit_receipt.await_count == 2
    async with maker() as session:
        state = (await session.get(Payment, pid)).meta[service.STATE_KEY]
        assert state["review_until"] == deadline and state["receipt_quote_revision"] == 2
        assert len(state["operations"]) == 3
