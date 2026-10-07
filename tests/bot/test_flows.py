"""End-to-end bot flows through the real dispatcher with a fake Telegram API."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select

from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Pay, Review, Shop, Wallet
from paurubot.config import get_settings
from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Customer,
    ForcedChannel,
    Job,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentStatus,
    Ticket,
)
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import (
    callback_update,
    make_bot,
    message_update,
    photo_update,
    png_bytes,
    user,
)

FA = Texts("fa")
UID = 7001


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    from sqlalchemy import update

    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Pauru Test")
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_accounts": [{"number": "6037990000000000", "holder": "Owner", "bank": "Melli"}]},
    )
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000, title="Monthly")
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "plan": plan, "bot": bot, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def _customer(session, shop_id: int, uid: int = UID) -> Customer:  # type: ignore[no-untyped-def]
    found = await customers.by_telegram(session, shop_id, uid)
    assert found is not None
    await session.refresh(found)
    return found


async def test_start_registers_customer_and_shows_menu(session, env) -> None:  # type: ignore[no-untyped-def]
    await env["send"](message_update(UID, "/start"))
    sent = env["fake"].last(SendMessage)
    assert "Pauru Test" in sent.text
    keyboard = [b.text for row in sent.reply_markup.keyboard for b in row]
    assert FA("btn_buy") in keyboard and FA("btn_admin") not in keyboard
    assert await _customer(session, env["shop"].id) is not None


async def test_referral_link_is_recorded(session, env) -> None:  # type: ignore[no-untyped-def]
    await env["send"](message_update(UID, "/start"))
    await env["send"](message_update(UID + 1, f"/start ref_{UID}"))
    friend = await _customer(session, env["shop"].id, UID + 1)
    referrer = await _customer(session, env["shop"].id)
    assert friend.referrer_id == referrer.id


async def test_buy_with_wallet_queues_provisioning(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_buy")))
    plans = fake.buttons(fake.last(SendMessage))
    assert any("100,000" in text for text, _ in plans)
    await send(callback_update(UID, Shop(step="plan", plan=env["plan"].id).pack()))
    checkout = fake.last(EditMessageText)
    labels = [text for text, _ in fake.buttons(checkout)]
    assert FA("btn_pay_wallet") not in labels and FA("btn_pay_card") in labels
    order = await session.scalar(select(Order).order_by(Order.id.desc()))
    customer = await _customer(session, env["shop"].id)
    await wallet.topup(session, env["shop"].id, customer.id, 150_000, provider="card", payment_id=99_001)
    await session.commit()
    await send(callback_update(UID, Pay(method="wallet", order=order.id).pack()))
    await session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await wallet_balance(session, env["shop"].id, customer.id) == 50_000
    job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{order.id}:provision"))
    assert job is not None


async def test_card_topup_receipt_then_owner_approves(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    owner = 9_900_001
    monkeypatch.setattr(get_settings(), "owner_telegram_ids", [owner])
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, Wallet(action="amount", amount=100_000).pack()))
    await send(callback_update(UID, Pay(method="card", amount=100_000).pack()))
    assert "6037990000000000" in fake.last(SendMessage).text
    fake.files["receipt-1"] = png_bytes()
    await send(photo_update(UID, "receipt-1"))
    payment = await session.scalar(select(Payment).order_by(Payment.id.desc()))
    assert payment.status == PaymentStatus.VERIFYING and payment.receipt_sha256
    review = await session.scalar(select(Job).where(Job.dedupe_key == f"payment:{payment.id}:review"))
    assert review is not None
    await send(message_update(owner, "/start", from_user=None))
    await send(callback_update(owner, Review(action="ok", id=payment.id).pack()))
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID and payment.fulfilled_at is not None
    customer = await _customer(session, env["shop"].id)
    assert await wallet_balance(session, env["shop"].id, customer.id) == 100_000


async def test_non_staff_cannot_review_payments(session, env) -> None:  # type: ignore[no-untyped-def]
    send = env["send"]
    await send(message_update(UID, "/start"))
    customer = await _customer(session, env["shop"].id)
    payment = Payment(
        shop_id=env["shop"].id,
        customer_id=customer.id,
        purpose="topup",
        provider="card",
        status=PaymentStatus.VERIFYING,
        amount=50_000,
        idempotency_key="manual-test-1",
    )
    session.add(payment)
    await session.commit()
    await send(callback_update(UID, Review(action="ok", id=payment.id).pack()))
    await session.refresh(payment)
    assert payment.status == PaymentStatus.VERIFYING


async def test_approve_button_never_settles_a_gateway_payment(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Only receipts and parked transfers are reviewed; a gateway payment waits for its gateway."""
    send = env["send"]
    owner = 9_900_002
    monkeypatch.setattr(get_settings(), "owner_telegram_ids", [owner])
    await send(message_update(UID, "/start"))
    customer = await _customer(session, env["shop"].id)
    payment = Payment(
        shop_id=env["shop"].id,
        customer_id=customer.id,
        purpose="topup",
        provider="zarinpal",
        status=PaymentStatus.PENDING,
        amount=50_000,
        idempotency_key="manual-test-gateway",
    )
    session.add(payment)
    await session.commit()
    await send(message_update(owner, "/start", from_user=None))
    await send(callback_update(owner, Review(action="ok", id=payment.id).pack()))
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PENDING
    assert await wallet_balance(session, env["shop"].id, customer.id) == 0


async def test_names_with_html_do_not_break_messages(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start", from_user=user(UID, name="<b>Tom & Jerry")))
    assert "&lt;b&gt;Tom &amp; Jerry" in fake.last(SendMessage).text


async def test_miniapp_button_answers_with_an_inline_web_app_button(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_miniapp")))
    markup = fake.last(SendMessage).reply_markup
    button = markup.inline_keyboard[0][0]
    assert button.web_app is not None and button.web_app.url == "https://bot.example.com/app/"


async def test_banned_and_forced_join(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    session.add(
        ForcedChannel(
            shop_id=env["shop"].id, chat_id="@pauru_news", title="News", url="https://t.me/pauru_news"
        )
    )
    await session.commit()
    fake.non_members.add(("@pauru_news", UID))
    await send(message_update(UID, FA("btn_buy")))
    assert fake.last(SendMessage).text == FA("join_required")
    customer = await _customer(session, env["shop"].id)
    customer.is_banned = True
    await session.commit()
    await send(message_update(UID, "/start"))
    assert fake.last(SendMessage).text == FA("banned")


async def test_trial_and_ticket(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_trial")))
    assert fake.last(SendMessage).text == FA("trial_processing")
    trial = await session.scalar(select(Order).where(Order.kind == OrderKind.TRIAL).order_by(Order.id.desc()))
    assert trial is not None
    await send(message_update(UID, FA("btn_trial")))
    assert fake.last(SendMessage).text == FA("trial_already_used")
    await send(callback_update(UID, "t:new:0:0"))
    await send(message_update(UID, "Connection problem"))
    await send(message_update(UID, "My config does not connect"))
    ticket = await session.scalar(select(Ticket).order_by(Ticket.id.desc()))
    assert ticket is not None and ticket.subject == "Connection problem"


async def test_language_switch(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, "l:en"))
    customer = await _customer(session, env["shop"].id)
    assert customer.locale == "en"
    await send(message_update(UID, Texts("en")("btn_wallet")))
    assert "Wallet" in fake.last(SendMessage).text
    assert any(isinstance(c, AnswerCallbackQuery) for c in fake.calls)


async def test_info_card_button_sends_a_picture(session, env) -> None:  # type: ignore[no-untyped-def]
    from aiogram.methods import SendPhoto

    from paurubot.bot.callbacks import Svc
    from paurubot.db.models import Service, ServiceStatus

    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    customer = await _customer(session, env["shop"].id)
    service = Service(
        shop_id=env["shop"].id,
        customer_id=customer.id,
        panel_id=env["plan"].panel_id,
        panel_username="u_card",
        label="Laptop",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * 1024**3,
        used_bytes=3 * 1024**3,
        subscription_url="https://panel.example/sub/u_card",
    )
    session.add(service)
    await session.commit()
    await send(callback_update(UID, Svc(action="card", id=service.id).pack()))
    photo = fake.last(SendPhoto)
    assert photo.chat_id == UID and photo.photo.data.startswith(b"\x89PNG")
    assert "Laptop" in photo.caption

    await shop_settings.set_many(session, env["shop"].id, {"info_card_enabled": False})
    await session.commit()
    shop_settings.invalidate(env["shop"].id)
    before = len([c for c in fake.calls if isinstance(c, SendPhoto)])
    await send(callback_update(UID, Svc(action="card", id=service.id).pack()))
    assert len([c for c in fake.calls if isinstance(c, SendPhoto)]) == before
