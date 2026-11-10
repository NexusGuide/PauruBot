"""A customer's dedicated bot is a selling bot only (OPB): it speaks the main shop's language,
has no language or reseller buttons, and the main shop's owners are not its admins."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from paurubot.bot.app import build_dispatcher
from paurubot.config import get_settings
from paurubot.core import own_panel, resale
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Job, JobStatus, PanelKind
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.security.secretbox import encrypt
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot, message_update, user

FA, EN = Texts("fa"), Texts("en")
TOKEN = "887766554:AAH-own-panel-bot-token-for-tests-only-x"
MERCHANT, BUYER, PLATFORM_OWNER = 71001, 72002, 5550001


class _EveryPanel(dict):  # type: ignore[type-arg]
    def __init__(self, shared) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.shared = shared

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        return self.shared


@pytest.fixture
async def own(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))

    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "own_bot"

    monkeypatch.setattr(resale, "bot_identity", identity)
    monkeypatch.setattr(get_settings(), "owner_telegram_ids", [PLATFORM_OWNER])
    monkeypatch.setattr(registry, "test_transports", _EveryPanel(transport(FakeTokenPanel("pasarguard"))))
    main = await factories.shop(session, "Main")
    await shop_settings.set_many(session, main.id, {"languages": ["fa"]})  # the main shop is Persian only
    panel = await factories.panel(session, main, PanelKind.PASARGUARD)
    plan = await factories.account_plan(session, main, panel)
    owner = await factories.customer(session, main, telegram_id=MERCHANT)
    account = await factories.panel_account(session, owner, plan)
    account.password_enc = encrypt("panel", "panel-password")
    child = await own_panel.connect(session, account, TOKEN)
    await session.commit()
    shop_settings.invalidate()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)

    async def send(shop_id, update):  # type: ignore[no-untyped-def]
        await build_dispatcher(MemoryStorage(), shop_id=shop_id).feed_update(bot, update)

    yield {"main": main, "child": child, "fake": fake, "send": send}
    notify_telegram.set_bot(None)
    registry._cache.clear()
    shop_settings.invalidate()


def _menu(fake) -> list[str]:  # type: ignore[no-untyped-def]
    markup = fake.last(SendMessage).reply_markup
    rows = getattr(markup, "keyboard", None) or getattr(markup, "inline_keyboard", None) or []
    return [button.text for row in rows for button in row]


async def test_the_bot_speaks_the_main_shops_language_and_has_no_language_button(session, own) -> None:  # type: ignore[no-untyped-def]
    """The main shop is Persian only: a buyer whose Telegram is in English still gets Persian."""
    send, fake, child = own["send"], own["fake"], own["child"]
    english = user(BUYER, lang="en")
    await send(child.id, message_update(BUYER, "/start", from_user=english))
    welcome = fake.last(SendMessage)
    assert FA("btn_buy") in _menu(fake) and EN("btn_buy") not in _menu(fake)
    assert FA("btn_language") not in _menu(fake) and EN("btn_language") not in _menu(fake)
    assert welcome.text and "خوش" in welcome.text
    customer = await session.scalar(
        select(Customer).where(Customer.shop_id == child.id, Customer.telegram_id == BUYER)
    )
    assert customer is not None and customer.locale == "fa"
    await send(child.id, message_update(BUYER, "/language", from_user=english))
    assert fake.last(SendMessage).text != FA("choose_language")
    # a buyer whose language was saved before keeps getting the shop's language
    await session.execute(
        update(Customer)
        .where(Customer.shop_id == child.id, Customer.telegram_id == BUYER)
        .values(locale="en")
    )
    await session.commit()
    await send(child.id, message_update(BUYER, "/start", from_user=english))
    assert FA("btn_buy") in _menu(fake)


async def test_the_main_shops_owner_is_a_customer_in_a_customers_bot(session, own) -> None:  # type: ignore[no-untyped-def]
    send, fake = own["send"], own["fake"]
    await send(own["child"].id, message_update(PLATFORM_OWNER, "/start"))
    assert FA("btn_admin") not in _menu(fake)
    await send(own["child"].id, message_update(PLATFORM_OWNER, FA("btn_admin")))
    assert fake.last(SendMessage).text != FA("adm_home")
    # ... and still the admin of the main shop
    await send(own["main"].id, message_update(PLATFORM_OWNER, "/start"))
    assert FA("btn_admin") in _menu(fake)


async def test_no_reseller_button_or_flow_in_a_customers_bot(session, own) -> None:  # type: ignore[no-untyped-def]
    send, fake = own["send"], own["fake"]
    await send(own["child"].id, message_update(BUYER, "/start"))
    assert FA("btn_reseller") not in _menu(fake)
    await send(own["child"].id, message_update(BUYER, FA("btn_reseller")))
    assert fake.last(SendMessage).text == FA("unknown_command")
    # the main shop keeps its reseller button
    await send(own["main"].id, message_update(BUYER, "/start"))
    assert FA("btn_reseller") in _menu(fake)


async def test_the_bot_stops_selling_when_its_time_is_up_and_warns_before(session, own) -> None:  # type: ignore[no-untyped-def]
    child = own["child"]
    end = await own_panel.set_expiry(session, child, 2)
    await session.commit()
    assert end is not None and timedelta(days=1) < end - utcnow() <= timedelta(days=2)
    assert await own_panel.recheck(session, child, verify=False) is None  # still selling
    assert await own_panel.expiry_warning(session, child) == "own_bot_expiring"
    assert await own_panel.expiry_warning(session, child) is None  # once per end date
    # the end passes: new sales, renewals and trials stop, with an alert
    await shop_settings.set_many(
        session, child.id, {own_panel.EXPIRES: (utcnow() - timedelta(minutes=1)).isoformat()}
    )
    assert await own_panel.recheck(session, child, verify=False) == "own_paused_bot_expired"
    assert await own_panel.sales_gate(session, child.id, new_user=True) == "own_sales_paused"
    assert await own_panel.sales_gate(session, child.id, new_user=False) == "own_sales_paused"
    # more days bring it back
    await own_panel.set_expiry(session, child, 30)
    assert await own_panel.recheck(session, child) == "own_resumed"
    assert await own_panel.sales_gate(session, child.id, new_user=True) is None
    await own_panel.set_expiry(session, child, None)
    assert own_panel.bot_expires_at(await shop_settings.load(session, child.id, fresh=True)) is None


async def test_a_stopped_bot_gets_no_reminder_about_its_end(session, own) -> None:  # type: ignore[no-untyped-def]
    child = own["child"]
    await own_panel.set_expiry(session, child, 2)
    await own_panel.disconnect(session, child)
    await session.commit()
    changes = await own_panel.recheck_all(session)
    assert all(alert != "own_bot_expiring" for _, alert in changes)
