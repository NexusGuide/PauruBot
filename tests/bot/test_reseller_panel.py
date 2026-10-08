"""The reseller's panel in the bot, through the real dispatcher (TEN-13)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage

from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Rs
from paurubot.db.base import utcnow
from paurubot.db.models import ResellerGroup, ResellerProfile, Service, ServiceStatus
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import GB
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
UID = 7301


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Pauru Resell")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000, title="Monthly")
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def _make_reseller(session, env):  # type: ignore[no-untyped-def]
    await env["send"](message_update(UID, "/start"))
    customer = await _customer(session, env["shop"].id, UID)
    group = ResellerGroup(shop_id=env["shop"].id, title="Silver", discount_percent=20)
    session.add(group)
    await session.flush()
    session.add(
        ResellerProfile(
            shop_id=env["shop"].id, customer_id=customer.id, group_id=group.id, permissions=["resellers.sub"]
        )
    )
    session.add(
        Service(
            shop_id=env["shop"].id,
            customer_id=customer.id,
            panel_id=env["panel"].id,
            plan_id=env["plan"].id,
            panel_username="u_client1",
            label="client one",
            status=ServiceStatus.ACTIVE,
            data_limit=10 * GB,
            expire_at=utcnow() + timedelta(days=2),
        )
    )
    await session.commit()
    return customer


async def test_reseller_button_opens_the_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await _make_reseller(session, env)
    await send(message_update(UID, FA("btn_reseller")))
    sent = fake.last(SendMessage)
    labels = [text for text, _ in fake.buttons(sent)]
    for key in ("rs_btn_sell", "rs_btn_search", "rs_btn_expiring", "rs_btn_report", "rs_btn_subs"):
        assert FA(key) in labels, key
    assert "20" in sent.text  # the group discount


async def test_search_and_expiring_from_the_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await _make_reseller(session, env)
    await send(callback_update(UID, Rs(action="search").pack()))
    await send(message_update(UID, "client"))
    found = [text for text, _ in fake.buttons(fake.last(SendMessage))]
    assert any("client one" in text for text in found)
    await send(callback_update(UID, Rs(action="expiring", value=3).pack()))
    listing = fake.last(EditMessageText)
    labels = [text for text, _ in fake.buttons(listing)]
    assert any("client one" in text for text in labels) and FA("rs_btn_renew_all") in labels


async def test_ordinary_customer_still_applies(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID + 5, "/start"))
    await send(message_update(UID + 5, FA("btn_reseller")))
    assert FA("rs_btn_search") not in [text for text, _ in fake.buttons(fake.last(SendMessage))]


async def test_web_panel_button_gives_the_sign_in(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await _make_reseller(session, env)
    await send(message_update(UID, FA("btn_reseller")))
    assert FA("rs_btn_web") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID, Rs(action="web").pack()))
    text = fake.last(EditMessageText).text
    assert f"r{UID}" in text and "/admin/" in text
