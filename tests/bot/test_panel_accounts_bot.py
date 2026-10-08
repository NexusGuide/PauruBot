"""Buying and managing a panel admin account in the bot, through the real dispatcher (TEN-14)."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import update

import paurubot.notifications.handlers  # noqa: F401  (registers the notify queue)
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Acc, Pay
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc
from paurubot.db.models import AccountPlan, Job, JobStatus, Order, PanelAccount, PanelKind
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_resellers import drain

FA = Texts("fa")
UID = 7401


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Pauru Admins")
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    fake_panel = FakeTokenPanel("pasarguard")
    registry.test_transports[panel.id] = transport(fake_panel)
    plan = AccountPlan(
        shop_id=shop.id, panel_id=panel.id, title="Pro admin", role_id=2, data_gb=300, days=30, price=800_000
    )
    session.add(plan)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "plan": plan, "fake": fake, "panel": fake_panel, "send": send}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def test_buy_an_admin_account_and_get_the_login(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    await send(message_update(UID, "/start"))
    menu = fake.last(SendMessage).reply_markup
    assert FA("btn_accounts") in [b.text for row in menu.keyboard for b in row]
    customer = await _customer(session, shop.id, UID)
    await wallet.topup(session, shop.id, customer.id, 1_000_000, provider="card", payment_id=customer.id)
    await session.commit()
    await send(message_update(UID, FA("btn_accounts")))
    assert FA("acc_btn_buy") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID, Acc(action="plans").pack()))
    assert any("Pro admin" in text for text, _ in fake.buttons(fake.last(EditMessageText)))
    await send(callback_update(UID, Acc(action="buy", id=plan.id).pack()))
    checkout = fake.last(EditMessageText)
    assert "800,000" in checkout.text or "۸۰۰" in checkout.text
    pay = next(data for text, data in fake.buttons(checkout) if text == FA("btn_pay_wallet"))
    assert Pay.unpack(pay).method == "wallet"
    await send(callback_update(UID, pay))
    await drain(session, "provision")
    await drain(session, "notify")
    account = (await session.execute(PanelAccount.__table__.select())).first()
    assert account is not None
    password = esc(env["panel"].sold_admins[account.username]["password"])  # HTML, as sent
    login = next(c for c in fake.calls if isinstance(c, SendMessage) and password in (c.text or ""))
    assert account.username in login.text and login.protect_content is True
    order = (await session.execute(Order.__table__.select().where(Order.customer_id == customer.id))).first()
    assert order is not None and order.status == "completed"
    # The account card, with its actions.
    await send(callback_update(UID, Acc(action="view", id=account.id).pack()))
    labels = [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    for key in ("acc_btn_login", "acc_btn_renew", "acc_btn_password", "acc_btn_pause"):
        assert FA(key) in labels, key


async def test_someone_else_cannot_open_the_account(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    await send(message_update(UID + 1, "/start"))
    owner = await _customer(session, shop.id, UID + 1)
    account = PanelAccount(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username="r1_ab12",
        password_enc="x",
    )
    session.add(account)
    await session.commit()
    await send(message_update(UID + 2, "/start"))
    fake.reset()
    await send(callback_update(UID + 2, Acc(action="login", id=account.id).pack()))
    assert not [c for c in fake.calls if isinstance(c, SendMessage)]
