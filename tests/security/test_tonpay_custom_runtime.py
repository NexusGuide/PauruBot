"""Current-quote observations, configuration gates and fail-closed reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments import tonpay_custom_runtime as runtime
from paurubot.payments import tonpay_custom_service as mutations
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.tonpay_custom import CustomAPIError, CustomInvoice
from tests.security.test_payment_intents import intent_engine as intent_engine
from tests.security.test_tonpay_custom_create import creation as creation
from tests.security.test_tonpay_custom_create import start

pytestmark = pytest.mark.security


@pytest.fixture
async def live(creation, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, config, client = creation
    result = await start(creation)
    cfg = {
        "tonpay_custom_enabled": True,
        "tonpay_custom_policy_approved": True,
        "tonpay_custom_api_key": "synthetic-only",
        "tonpay_custom_credential_epoch": config.credential_epoch,
        "tonpay_custom_confirm_enabled": False,
        "payment_expire_minutes": 1440,
        "tonpay_custom_receipt_wait": 600,
        "tonpay_custom_card_cooldown": 60,
        "tonpay_custom_review_window": 3600,
    }
    monkeypatch.setattr(runtime, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(public_url="https://example.com"))
    monkeypatch.setattr(runtime.shop_settings, "load", AsyncMock(side_effect=lambda *a, **k: dict(cfg)))
    monkeypatch.setattr(engine, "topup_bounds", AsyncMock(return_value=(10000, 1000000)))
    monkeypatch.setattr(runtime, "TonpayCustomClient", lambda key: client)
    invoice = CustomInvoice(
        "invoice_" + str(result.payment_id),
        str(result.payment_id),
        100000,
        103000,
        "pending",
        False,
        "0000000000000000",
        "Synthetic",
    )
    monkeypatch.setattr(client, "check", AsyncMock(return_value=invoice))
    audit = AsyncMock()
    monkeypatch.setattr(runtime, "record", audit)
    paid = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "mark_paid", paid)
    return maker, owner, result.payment_id, client, invoice, cfg, audit, paid


async def change(live, fn):  # type: ignore[no-untyped-def]
    maker, _, pid, *_ = live
    async with maker() as session:
        p = await session.get(Payment, pid)
        fn(p)
        await session.commit()


async def test_card_is_kept_encrypted_and_only_shown_after_exact_consent(live):  # type: ignore[no-untyped-def]
    maker, owner, pid, client, _, _, _, paid = live
    assert "card" not in await runtime.refresh(owner, pid)
    assert "card" not in await runtime.view(owner, pid)
    await mutations.consent(owner, pid, 1, 103000)
    assert (await runtime.refresh(owner, pid))["card"]["number"] == "0000000000000000"
    assert (await runtime.view(owner, pid))["card"]["number"] == "0000000000000000"
    async with maker() as session:
        p = await session.get(Payment, pid)
        assert "0000000000000000" not in str(p.meta) and "Synthetic" not in str(p.meta)
        assert p.status == PaymentStatus.PENDING and p.amount == 100000
    assert client.check.await_count == 2
    paid.assert_not_awaited()


@pytest.mark.parametrize("kind", ["fee", "card"])
async def test_changed_quote_clears_consent_and_hides_card(live, kind):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, *_ = live
    await runtime.refresh(owner, pid)
    await mutations.consent(owner, pid, 1, 103000)
    client.check.return_value = replace(
        invoice, **({"payable_amount": 104000} if kind == "fee" else {"card_number": "1111111111111111"})
    )
    result = await runtime.refresh(owner, pid)
    assert result["quote_revision"] == 2 and not result["consented"] and "card" not in result


async def test_consent_or_local_mutation_fences_an_old_get(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, *_ = live
    entered, release = asyncio.Event(), asyncio.Event()

    async def remote(*args):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return invoice

    client.check.side_effect = remote
    task = asyncio.create_task(runtime.refresh(owner, pid))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await mutations.consent(owner, pid, 1, 103000)
    finally:
        release.set()
    with pytest.raises(mutations.CustomStateError) as exc:
        await asyncio.wait_for(task, 5)
    assert exc.value.code == "observation_stale"
    assert (await runtime.view(owner, pid))["consented"]


async def test_newer_get_claim_fences_previous_reply(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, *_ = live
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def remote(*args):  # type: ignore[no-untyped-def]
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            return invoice
        return replace(invoice, payable_amount=105000)

    client.check.side_effect = remote
    task = asyncio.create_task(runtime.refresh(owner, pid))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await runtime.refresh(owner, pid))["payable_amount"] == 105000
    finally:
        release.set()
    with pytest.raises(mutations.CustomStateError):
        await asyncio.wait_for(task, 5)
    assert (await runtime.view(owner, pid))["payable_amount"] == 105000


async def test_read_cannot_clear_or_attribute_an_uncertain_mutation(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, _, _, paid = live
    client.change_card = AsyncMock(side_effect=CustomAPIError("upstream_error"))
    with pytest.raises(CustomAPIError):
        await mutations.change_card(owner, pid, "ambiguous-card-key-001", 1, client=client)
    client.check.return_value = replace(invoice, status="completed", paid=True, receipt_received=True)
    result = await runtime.refresh(owner, pid)
    assert result["uncertain"] and result["recovery"] == "operator_review_required"
    assert result["provider_paid"] is False and "card" not in result
    assert not result["can_change_card"] and not result["can_receipt"]
    paid.assert_not_awaited()


async def test_unknown_creation_reference_never_guessed_or_reposted(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, *_ = live

    def unbind(p):  # type: ignore[no-untyped-def]
        p.provider_ref = None
        p.meta = {
            **p.meta,
            "tonpay_custom_create_v1": {**p.meta["tonpay_custom_create_v1"], "state": "uncertain"},
        }

    await change(live, unbind)
    result = await runtime.refresh(owner, pid)
    assert result["recovery"] == "provider_lookup_unavailable" and result["uncertain"]
    assert (await runtime.resume(owner, 100000, "custom-create-test-0001"))["payment_id"] == pid
    client.check.assert_not_awaited()
    client.create.assert_awaited_once()  # initial known fixture only


@pytest.mark.parametrize(
    "case",
    [
        "flag_off",
        "not_paid",
        "not_completed",
        "fee_changed",
        "expired",
        "closed",
        "no_consent",
        "gate_changed",
        "epoch_changed",
    ],
)
async def test_confirmation_requires_all_current_gates(live, case):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, cfg, _, paid = live
    cfg["tonpay_custom_confirm_enabled"] = case != "flag_off"
    if case != "no_consent":
        await mutations.consent(owner, pid, 1, 103000)
    reply = replace(
        invoice, paid=case != "not_paid", status="pending" if case == "not_completed" else "completed"
    )
    if case == "fee_changed":
        reply = replace(reply, payable_amount=104000)
    if case == "expired":
        await change(live, lambda p: setattr(p, "expires_at", utcnow() - timedelta(seconds=1)))
    if case == "closed":
        await change(live, lambda p: setattr(p, "status", PaymentStatus.FAILED))

    async def remote(*args):  # type: ignore[no-untyped-def]
        if case == "gate_changed":
            cfg["tonpay_custom_confirm_enabled"] = False
        if case == "epoch_changed":
            cfg["tonpay_custom_credential_epoch"] = "another-key-epoch-0001"
        return reply

    client.check.side_effect = remote
    await runtime.refresh(owner, pid)
    paid.assert_not_awaited()


async def test_provider_confirmation_calls_existing_engine_with_credit_not_fee(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, cfg, audit, paid = live
    cfg["tonpay_custom_confirm_enabled"] = True
    await mutations.consent(owner, pid, 1, 103000)
    client.check.return_value = replace(invoice, paid=True, status="completed")
    await runtime.refresh(owner, pid)
    assert paid.await_args.kwargs == {"provider_tx_id": "custom:" + invoice.invoice_id, "amount": 100000}
    assert paid.await_args.args[1] == pid
    assert audit.await_args.kwargs["action"] == "tonpay_custom.confirmed"


async def test_paid_observation_is_not_downgraded_by_unpaid_get(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, invoice, *_ = live
    client.check.return_value = replace(invoice, paid=True, status="completed")
    await runtime.refresh(owner, pid)
    client.check.return_value = invoice
    result = await runtime.refresh(owner, pid)
    assert result["provider_paid"] and result["provider_status"] == "completed" and "card" not in result


async def test_rotation_disables_old_payment_operations_before_http(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, _, cfg, *_ = live
    cfg["tonpay_custom_credential_epoch"] = "another-key-epoch-0001"
    with pytest.raises(mutations.CustomStateError) as exc:
        await runtime.refresh(owner, pid)
    assert exc.value.code == "credential_changed"
    client.check.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tonpay_custom_enabled", False),
        ("tonpay_custom_policy_approved", False),
        ("tonpay_custom_api_key", ""),
        ("tonpay_custom_receipt_wait", 0),
        ("tonpay_custom_receipt_wait", True),
        ("tonpay_custom_credential_epoch", "short"),
        ("payment_expire_minutes", 1),
    ],
)
async def test_invalid_runtime_policy_is_not_available(live, field, value):  # type: ignore[no-untyped-def]
    maker, owner, _, _, _, cfg, *_ = live
    cfg[field] = value
    with pytest.raises(PaymentError):
        await runtime.runtime(owner)
    async with maker() as session:
        from paurubot.db.models import Customer

        assert not await runtime.available(session, await session.get(Customer, owner.customer_id))


async def test_resume_is_scoped_and_cannot_bind_a_reference(live):  # type: ignore[no-untyped-def]
    _, owner, pid, client, *_ = live
    assert (await runtime.resume(owner, 100000, "custom-create-test-0001"))["payment_id"] == pid
    for other, amount, key in [
        (mutations.Owner(owner.shop_id, owner.customer_id + 1000), 100000, "custom-create-test-0001"),
        (owner, 100001, "custom-create-test-0001"),
        (owner, 100000, "different-owned-key-001"),
    ]:
        with pytest.raises(mutations.CustomStateError):
            await runtime.resume(other, amount, key)
    client.check.assert_not_awaited()


async def test_recent_returns_no_provider_reference_or_internal_token(live):  # type: ignore[no-untyped-def]
    _, owner, pid, *_ = live
    rows = await runtime.recent(owner)
    assert len(rows) == 1 and rows[0]["payment_id"] == pid
    assert "invoice_id" not in rows[0] and "provider_ref" not in rows[0] and "token" not in str(rows)


async def block_customer(maker, owner):  # type: ignore[no-untyped-def]
    from paurubot.db.models import Customer

    async with maker() as session:
        customer = await session.get(Customer, owner.customer_id)
        customer.card_trust = "blocked"
        await session.commit()


async def test_blocked_customer_cannot_acknowledge_quote(live):  # type: ignore[no-untyped-def]
    maker, owner, pid, *_ = live
    await block_customer(maker, owner)
    with pytest.raises(mutations.CustomStateError):
        await mutations.consent(owner, pid, 1, 103000)


@pytest.mark.parametrize("paid_reply", [False, True])
async def test_customer_blocked_during_get_cannot_receive_card_or_credit(live, paid_reply):  # type: ignore[no-untyped-def]
    maker, owner, pid, client, invoice, cfg, _, paid = live
    cfg["tonpay_custom_confirm_enabled"] = True
    await mutations.consent(owner, pid, 1, 103000)

    async def remote(*args):  # type: ignore[no-untyped-def]
        await block_customer(maker, owner)
        return replace(invoice, paid=paid_reply, status="completed" if paid_reply else "pending")

    client.check.side_effect = remote
    with pytest.raises(mutations.CustomStateError):
        await runtime.refresh(owner, pid)
    paid.assert_not_awaited()
    async with maker() as session:
        p = await session.get(Payment, pid)
        assert p.status == PaymentStatus.PENDING and not p.meta[mutations.STATE_KEY]["paid"]


@pytest.mark.parametrize(
    "remote_status", ["need_action", "action_required_less", "action_required_more", "processing"]
)
async def test_original_receipt_projection_disabled_outside_pending(live, remote_status):  # type: ignore[no-untyped-def]
    _, owner, pid, *_ = live
    await mutations.consent(owner, pid, 1, 103000)

    def change_status(payment):  # type: ignore[no-untyped-def]
        from copy import deepcopy

        state = deepcopy(payment.meta[mutations.STATE_KEY])
        state["remote_status"] = remote_status
        state["created_at"] = mutations._now() - state["policy"]["receipt_wait"] - 1
        payment.meta = {**payment.meta, mutations.STATE_KEY: state}

    await change(live, change_status)
    assert (await runtime.view(owner, pid))["can_receipt"] is False


async def test_card_from_creation_survives_a_check_without_card(live):  # type: ignore[no-untyped-def]
    """TonPays sends the card on creation only; a status check without it must not lose it."""
    maker, owner, pid, client, invoice, *_ = live
    client.check.return_value = replace(invoice, card_number="", card_name="")
    assert "card" not in await runtime.refresh(owner, pid)
    await mutations.consent(owner, pid, 1, 103000)
    assert (await runtime.view(owner, pid))["card"] == {"number": "6037990000000001", "holder": "Creator"}
    assert (await runtime.refresh(owner, pid))["card"]["number"] == "6037990000000001"
    async with maker() as session:
        p = await session.get(Payment, pid)
        assert "6037990000000001" not in str(p.meta) and "Creator" not in str(p.meta)


async def test_empty_status_from_a_check_keeps_the_invoice_open(live):  # type: ignore[no-untyped-def]
    """A check without a status says nothing new: the invoice stays open and the receipt can be sent."""
    _, owner, pid, client, invoice, *_ = live
    await mutations.consent(owner, pid, 1, 103000)

    def waited(p):  # type: ignore[no-untyped-def]
        state = dict(p.meta[mutations.STATE_KEY])
        state["created_at"] -= 3600
        p.meta = {**p.meta, mutations.STATE_KEY: state}

    await change(live, waited)
    client.check.return_value = replace(invoice, status="")
    result = await runtime.refresh(owner, pid)
    assert result["provider_status"] == "pending"
    assert result["can_receipt"] is True and result["receipt_received"] is False
