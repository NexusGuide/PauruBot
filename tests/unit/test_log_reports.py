"""Full reports to the shop's report group (SUP-03): every field staff look for, in the shop's
language, with a «manage user» button; newer topics fall back to broader ones."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

from paurubot.bot.app import build_dispatcher
from paurubot.config import get_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    DiscountCode,
    LogChannel,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Service,
    ServiceStatus,
    Staff,
    StaffRole,
)
from paurubot.i18n import Texts
from paurubot.notifications import handlers, log_reports
from paurubot.notifications import telegram as notify_telegram
from paurubot.worker.queue import Claimed
from tests import factories
from tests.fakes.telegram import make_bot, message_update

FA = Texts("fa")
GB = 1024**3


def job(kind: str, payload: dict[str, Any], shop_id: int) -> Claimed:
    return Claimed(id=1, kind=kind, payload=payload, attempts=1, max_attempts=5, shop_id=shop_id, token="t")


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Reports")
    shop.bot_username = "reports_bot"
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=120_000, days=30, data_gb=50, title="۵۰ گیگ")
    customer = await factories.customer(session, shop, telegram_id=8053694064, username="jaber")
    customer.phone = "+989121234567"
    session.add_all(
        [
            LogChannel(shop_id=shop.id, topic="orders", chat_id=-1001),
            LogChannel(shop_id=shop.id, topic="payments", chat_id=-1002),
            LogChannel(shop_id=shop.id, topic="all", chat_id=-1009),
        ]
    )
    await session.commit()
    sent: list[dict[str, Any]] = []

    async def capture(chat_id, text, **kwargs):  # type: ignore[no-untyped-def]
        sent.append({"chat": chat_id, "text": text, **kwargs})

    monkeypatch.setattr(notify_telegram, "send_text", capture)
    return {"shop": shop, "panel": panel, "plan": plan, "customer": customer, "sent": sent}


def manage_url(entry: dict[str, Any]) -> str:
    return entry["reply_markup"].inline_keyboard[0][0].url


async def test_a_purchase_report_has_every_field_and_a_manage_button(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, customer, plan = env["shop"], env["customer"], env["plan"]
    code = DiscountCode(shop_id=shop.id, code="OFF10", value=10)
    session.add(code)
    await session.flush()
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=env["panel"].id,
        plan_id=plan.id,
        panel_username="jaber_6806d5",
        status=ServiceStatus.ACTIVE,
        data_limit=50 * GB,
        expire_at=utcnow() + timedelta(days=30),
    )
    session.add(service)
    await session.flush()
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        plan_id=plan.id,
        service_id=service.id,
        base_price=120_000,
        code_discount=12_000,
        amount=108_000,
        discount_code_id=code.id,
    )
    session.add(order)
    await session.flush()
    session.add(
        Payment(
            shop_id=shop.id,
            customer_id=customer.id,
            order_id=order.id,
            purpose=PaymentPurpose.ORDER,
            provider="card",
            status=PaymentStatus.PAID,
            amount=108_000,
            idempotency_key="report-1",
        )
    )
    await session.commit()
    await session.refresh(order)
    topic, text, markup = await log_reports.service_order(session, order, service, customer)
    assert topic == "orders"
    for part in (
        "سفارش #",
        "@jaber",
        "8053694064",
        "+989121234567",
        "۵۰ گیگ",
        "jaber_6806d5",
        "50 گیگ",
        "30 روز",
        "108,000",
        "OFF10",
        "کارت به کارت",
    ):
        assert part in text, part
    assert (
        markup is not None
        and markup.inline_keyboard[0][0].url == f"https://t.me/reports_bot?start=cust_{customer.id}"
    )


async def test_a_trial_has_its_own_topic_falling_back_to_orders(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, customer = env["shop"], env["customer"]
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=env["panel"].id,
        panel_username="trial_8053",
        status=ServiceStatus.ACTIVE,
        data_limit=GB,
        is_trial=True,
    )
    session.add(service)
    await session.flush()
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.COMPLETED,
        service_id=service.id,
        params={"gb": 1, "days": 1},
    )
    session.add(order)
    await session.commit()
    topic, text, markup = await log_reports.service_order(session, order, service, customer)
    assert topic == "trials" and "trial_8053" in text and "1 گیگ" in text and "1 روز" in text
    await handlers.report(session, shop.id, topic, text, markup=markup)
    assert env["sent"][-1]["chat"] == -1001  # no trials chat yet: the orders one
    session.add(LogChannel(shop_id=shop.id, topic="trials", chat_id=-1003))
    await session.commit()
    await handlers.report(session, shop.id, topic, text, markup=markup)
    assert env["sent"][-1]["chat"] == -1003
    await handlers.report(session, shop.id, "users", "x")
    assert env["sent"][-1]["chat"] == -1009  # no users chat: "all"


async def test_a_rejected_receipt_is_reported_with_who_and_why(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, customer = env["shop"], env["customer"]
    staff = Staff(
        shop_id=shop.id, role=StaffRole.FINANCE, display_name="XSORA_ADMINS", telegram_id=6980608004
    )
    session.add(staff)
    await session.flush()
    payment = Payment(
        shop_id=shop.id,
        customer_id=customer.id,
        purpose=PaymentPurpose.TOPUP,
        provider="card",
        status=PaymentStatus.FAILED,
        amount=75_000,
        idempotency_key="report-2",
        reviewed_by_staff_id=staff.id,
        reviewed_at=utcnow(),
    )
    session.add(payment)
    await session.commit()
    await handlers.payment_rejected(
        session,
        job("payment_rejected", {"payment_id": payment.id, "reason": "به پشتیبانی پیام بدید"}, shop.id),
    )
    report = next(e for e in env["sent"] if e["chat"] == -1002)
    for part in (
        "75,000",
        "کارت به کارت",
        "XSORA_ADMINS",
        "6980608004",
        "به پشتیبانی پیام بدید",
        "8053694064",
    ):
        assert part in report["text"], part
    assert "cust_" in manage_url(report)


async def test_a_referral_commission_shows_both_customers_and_the_balance(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, buyer = env["shop"], env["customer"]
    referrer = await factories.customer(session, shop, telegram_id=6980608004, username="ref")
    order = Order(
        shop_id=shop.id,
        customer_id=buyer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        amount=50_000,
    )
    session.add(order)
    await session.commit()
    payload = {
        "amount": 5_000,
        "buyer_id": buyer.id,
        "order_id": order.id,
        "percent": 10,
        "balance_after": 155_000,
    }
    await handlers._referral_report(session, referrer, payload)
    report = env["sent"][-1]
    assert report["chat"] == -1002  # no referrals chat: the payments one
    for part in ("5,000", "10٪", "150,000", "155,000", "8053694064", "6980608004", f"#{order.id}"):
        assert part in report["text"], part


async def test_the_cleanup_report_says_why_and_what_was_left(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, customer = env["shop"], env["customer"]
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=env["panel"].id,
        panel_username="gladiatwr_5792846",
        status=ServiceStatus.DELETED,
        data_limit=10 * GB,
        used_bytes=10 * GB,
        limited_at=utcnow() - timedelta(days=8),
        expire_at=utcnow() + timedelta(days=5, hours=1),
        online_at=utcnow() - timedelta(days=9),
    )
    session.add(service)
    await session.commit()
    text = await log_reports.service_deleted(session, service, customer)
    for part in ("gladiatwr_5792846", FA("report_status_traffic"), "5", env["panel"].name):
        assert part in text, part


async def test_manage_user_opens_the_card_for_staff_only(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, customer = env["shop"], env["customer"]
    monkeypatch.setattr(get_settings(), "owner_telegram_ids", [5550001])
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    try:
        dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)
        await dp.feed_update(bot, message_update(5550001, f"/start cust_{customer.id}"))
        card = fake.last(SendMessage)
        assert str(customer.telegram_id) in (card.text or "")
        await dp.feed_update(bot, message_update(7777, f"/start cust_{customer.id}"))
        assert str(customer.telegram_id) not in (fake.last(SendMessage).text or "")
    finally:
        notify_telegram.set_bot(None)
