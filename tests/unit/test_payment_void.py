"""REF-01: staff can void a payment a person approved by mistake.

A fake card receipt that got approved used to stay paid for good: the purchase it bought kept
running and only a hand-written wallet adjustment could take anything back. A void now reverses
exactly what that approval gave (the wallet credit, the purchase it settled, the sale's side
payments) and turns the service off on its panel, or refuses as a whole and changes nothing.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from paurubot.api.app import create_app
from paurubot.audit.log import Actor
from paurubot.auth.staff import create_staff
from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Customer,
    Job,
    LedgerJournal,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    Payment,
    PaymentStatus,
    Service,
    ServiceStatus,
    StaffRole,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import system_account, wallet_account, wallet_balance
from paurubot.ledger.posting import reconcile, transfer
from paurubot.panels import registry
from paurubot.payments import engine, voids
from paurubot.payments.voids import VoidError
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit import test_provisioning
from tests.unit.test_provisioning import drain

fakes = test_provisioning.fakes
isolate_queue = test_provisioning.isolate_queue
STAFF = Actor("staff", None)
CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]
PASSWORD = "a-very-strong-pass"


async def _card_shop(session, fakes):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    fakes[panel.id] = fake
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS, "card_random_tail": False})
    buyer = await factories.customer(session, shop)  # no wallet money: the receipt is all they paid
    await session.commit()
    return shop, panel, plan, buyer, fake


async def _approved(session, payment_id: int) -> None:  # type: ignore[no-untyped-def]
    assert await engine.approve_card(session, payment_id, staff_id=None)  # type: ignore[arg-type]
    await session.commit()
    assert await engine.fulfill_one(session, payment_id) is not None


async def _pay_by_card(session, buyer, plan):  # type: ignore[no-untyped-def]
    """A purchase paid with a card receipt that staff approved, and its delivered service."""
    order = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    )
    started = await engine.pay_order(session, buyer, order, "card")
    await engine.attach_receipt(session, started.payment, file_id="f", text="fake", image=None)
    await session.commit()
    await _approved(session, started.payment.id)
    assert await drain(session, "provision") >= 1
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    return started.payment, order


async def _topup_by_card(session, buyer, amount: int) -> Payment:  # type: ignore[no-untyped-def]
    started = await engine.start(session, buyer, "card", amount)
    await engine.attach_receipt(session, started.payment, file_id="t", text="fake", image=None)
    await session.commit()
    await _approved(session, started.payment.id)
    return started.payment


async def _journal_count(session) -> int:  # type: ignore[no-untyped-def]
    return int(await session.scalar(select(func.count(LedgerJournal.id))) or 0)


async def test_fake_receipt_purchase_is_taken_back_and_its_service_stopped(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, fake = await _card_shop(session, fakes)
    payment, order = await _pay_by_card(session, buyer, plan)
    service = await session.get(Service, order.service_id)
    assert service is not None and service.status == ServiceStatus.ACTIVE
    assert voids.voidable(payment)

    result = await voids.void(
        session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt", block_card=True
    )
    await session.commit()
    assert result.refunded_order and result.service_id == service.id and result.kept == []
    await session.refresh(payment)
    await session.refresh(order)
    assert payment.status == PaymentStatus.VOIDED and payment.meta["void"]["reason"] == "fake_receipt"
    assert order.status == OrderStatus.REFUNDED
    # The customer is back where they started: no money, and no more card payments.
    assert await wallet_balance(session, shop.id, buyer.id) == 0
    await session.refresh(buyer)
    assert buyer.card_trust == "blocked"
    assert await reconcile(session, shop.id) == []
    notice = await session.scalar(select(Job).where(Job.dedupe_key == f"payment:{payment.id}:voided"))
    assert notice is not None and notice.kind == "payment_voided"

    # The panel is changed by a durable job, under the service lease.
    assert await drain(session, "provision") == 1
    await session.refresh(service)
    assert service.status == ServiceStatus.DISABLED and service.disabled_reason == "void"
    assert fake.users[service.panel_username]["status"] == "disabled"

    with pytest.raises(VoidError, match="void_already"):
        await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")


async def test_spent_credit_refuses_the_whole_void(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    payment = await _topup_by_card(session, buyer, 100_000)
    order = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)  # the fake credit is spent on another order
    await session.commit()
    journals = await _journal_count(session)

    with pytest.raises(VoidError) as caught:
        await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    assert caught.value.key == "void_wallet_spent" and caught.value.params["balance"] == 0
    await session.rollback()
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID
    assert await _journal_count(session) == journals  # nothing was posted, not even halfway


async def test_unspent_topup_and_its_bonus_are_reversed(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, _, buyer, _ = await _card_shop(session, fakes)
    await shop_settings.set_many(
        session, shop.id, {"topup_bonus_rules": [{"min": 50_000, "percent": 10, "provider": "card"}]}
    )
    payment = await _topup_by_card(session, buyer, 100_000)
    assert await wallet_balance(session, shop.id, buyer.id) == 110_000
    result = await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="duplicate")
    await session.commit()
    assert not result.refunded_order and result.service_id is None
    assert await wallet_balance(session, shop.id, buyer.id) == 0
    assert await reconcile(session, shop.id) == []


async def test_only_payments_a_person_approved_can_be_voided(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, _, buyer, _ = await _card_shop(session, fakes)
    shop_id, buyer_id = shop.id, buyer.id
    gateway = Payment(
        shop_id=shop.id,
        customer_id=buyer.id,
        purpose="topup",
        provider="zarinpal",
        status=PaymentStatus.CREATED,
        amount=50_000,
        idempotency_key="zarinpal:void-test",
    )
    session.add(gateway)
    await session.flush()
    assert await engine.mark_paid(session, gateway.id, provider_tx_id="zp-1")
    await session.commit()
    await engine.fulfill_one(session, gateway.id)
    await session.refresh(gateway)
    assert gateway.status == PaymentStatus.PAID and not voids.voidable(gateway)
    with pytest.raises(VoidError, match="void_not_voidable"):
        await voids.void(session, gateway.id, shop_id=shop_id, actor=STAFF, reason="mistake")
    await session.rollback()
    other = await factories.shop(session, "Other shop")
    card = await _topup_by_card(session, await session.get(Customer, buyer_id), 20_000)
    with pytest.raises(VoidError, match="void_not_found"):
        await voids.void(session, card.id, shop_id=other.id, actor=STAFF, reason="mistake")


async def test_a_later_paid_renewal_keeps_the_service_running(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    payment, order = await _pay_by_card(session, buyer, plan)
    service = await session.get(Service, order.service_id)
    await wallet.topup(session, shop.id, buyer.id, 100_000, provider="zarinpal", payment_id=10**8 + buyer.id)
    renew = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.RENEW, plan=plan, service=service)
    )
    await orders.settle_from_wallet(session, renew.id)
    await session.commit()
    with pytest.raises(VoidError) as caught:
        await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    assert caught.value.key == "void_service_changed" and caught.value.params["later"] == renew.id


async def test_an_order_still_being_delivered_waits(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    order = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    )
    started = await engine.pay_order(session, buyer, order, "card")
    await engine.attach_receipt(session, started.payment, file_id="f", text="fake", image=None)
    await session.commit()
    await _approved(session, started.payment.id)  # paid; the provisioning job has not run yet
    with pytest.raises(VoidError, match="void_order_in_progress"):
        await voids.void(session, started.payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")


async def test_referral_reward_is_taken_back_or_reported(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    await shop_settings.set_many(session, shop.id, {"referral_enabled": True, "referral_percent": 10})
    inviter = await factories.customer(session, shop)
    buyer.referrer_id = inviter.id
    await session.commit()
    payment, order = await _pay_by_card(session, buyer, plan)
    assert await wallet_balance(session, shop.id, inviter.id) == 10_000
    result = await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    await session.commit()
    assert result.kept == []
    assert await wallet_balance(session, shop.id, inviter.id) == 0
    await session.refresh(buyer)
    assert buyer.referral_rewarded is False  # a real first purchase still rewards the inviter

    # When the inviter already spent the reward, the void goes on and staff are told.
    second = await factories.customer(session, shop)
    second.referrer_id = inviter.id
    await session.commit()
    payment2, _ = await _pay_by_card(session, second, plan)
    await wallet.adjust(session, shop.id, inviter.id, -10_000, key="spent-reward", actor="test")
    await session.commit()
    result = await voids.void(session, payment2.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    await session.commit()
    assert result.kept == ["referral"]
    assert await wallet_balance(session, shop.id, second.id) == 0
    assert await reconcile(session, shop.id) == []


async def test_a_voided_purchase_no_longer_counts_toward_a_reward_config(session, fakes) -> None:  # type: ignore[no-untyped-def]
    """A fake receipt must not earn the inviter a free config (referral reward config)."""
    from paurubot.core import referral_rewards

    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "referral_enabled": True,
            "referral_reward_mode": "config",
            "referral_config_every": 1,
            "referral_config_plan_id": plan.id,
        },
    )
    inviter = await factories.customer(session, shop)
    buyer.referrer_id = inviter.id
    await session.commit()
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    payment, _ = await _pay_by_card(session, buyer, plan)
    assert (await referral_rewards.status(session, inviter, cfg)).available == 1
    await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    await session.commit()
    await session.refresh(buyer)
    assert buyer.referral_counted_at is None
    assert (await referral_rewards.status(session, inviter, cfg)).available == 0


async def test_a_partial_cancellation_refund_is_not_paid_twice(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    payment, order = await _pay_by_card(session, buyer, plan)
    service = await session.get(Service, order.service_id)
    assert service is not None
    # The customer deleted the service and got 40,000 of the 100,000 back (core.cancellation).
    revenue = await system_account(session, shop.id, "revenue")
    target = await wallet_account(session, shop.id, buyer.id)
    await transfer(
        session,
        shop_id=shop.id,
        source_id=revenue.id,
        target_id=target.id,
        amount=40_000,
        kind="refund",
        idempotency_key=f"service:{service.id}:cancel",
        reference_type="service",
        reference_id=service.id,
    )
    service.status = ServiceStatus.DELETED
    await session.commit()
    result = await voids.void(session, payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    await session.commit()
    assert result.refunded_order
    assert await wallet_balance(session, shop.id, buyer.id) == 0
    stop = await session.scalar(select(Job).where(Job.dedupe_key == f"payment:{payment.id}:void-service"))
    assert stop is None  # nothing left to turn off
    assert await reconcile(session, shop.id) == []


async def test_a_voided_receipt_stops_automatic_card_approval(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, _, buyer, _ = await _card_shop(session, fakes)
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_auto_approve": True, "card_auto_max_amount": 1_000_000, "card_auto_min_history": 0},
    )
    first = await engine.start(session, buyer, "card", 30_000)
    await engine.attach_receipt(session, first.payment, file_id="first", text="fake", image=None)
    await session.commit()
    assert first.payment.status == PaymentStatus.PAID  # approved automatically
    await engine.fulfill_one(session, first.payment.id)
    await voids.void(session, first.payment.id, shop_id=shop.id, actor=STAFF, reason="fake_receipt")
    await session.commit()
    started = await engine.start(session, buyer, "card", 30_000)
    await engine.attach_receipt(session, started.payment, file_id="again", text="again", image=None)
    await session.commit()
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.VERIFYING  # a person reviews it now


async def test_web_void_needs_the_refund_permission(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _, plan, buyer, _ = await _card_shop(session, fakes)
    payment, order = await _pay_by_card(session, buyer, plan)
    for role, name in ((StaffRole.ADMIN, "void_admin"), (StaffRole.FINANCE, "void_finance")):
        await create_staff(session, shop.id, role=role, username=name, password=PASSWORD)
    await session.commit()
    transport = httpx.ASGITransport(app=create_app(), client=("10.1.1.2", 5000))
    results = {}
    for name in ("void_admin", "void_finance"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
            assert login.status_code == 200, login.text
            headers = {"x-csrf-token": login.json()["csrf_token"]}
            listed = await client.get(f"/api/admin/payments?id={payment.id}", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["items"][0]["voidable"] is True
            results[name] = await client.post(
                f"/api/admin/payments/{payment.id}/void",
                json={"reason": "fake_receipt", "note": "edited screenshot"},
                headers=headers,
            )
    assert results["void_admin"].status_code == 403  # ADMIN does not hold payments.refund
    body = results["void_finance"].json()
    assert results["void_finance"].status_code == 200, body
    assert body["payment"]["status"] == "voided" and body["payment"]["void"]["note"] == "edited screenshot"
    assert body["order_id"] == order.id and body["kept"] == []
    await session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
    refused = await session.get(Order, order.id)
    assert refused is not None
