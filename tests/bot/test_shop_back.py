"""The back button while buying always goes somewhere: one step up, or out to the main menu from the
first screen. It used to point the first screen back at itself (one location, no categories), so
Telegram refused the identical edit and the button did nothing."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage

from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Shop
from paurubot.db.models import Category, Location
from paurubot.i18n import Texts
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
UID = 7301


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Back Test")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=30_000, title="Test")
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake, "send": send}
    await bot.session.close()


def _back(fake, call) -> str:  # type: ignore[no-untyped-def]
    data = [cb for text, cb in fake.buttons(call) if text == FA("btn_back")]
    assert data, "the screen has a back button"
    return str(data[0])


async def _press_back_to_menu(env, back: str) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    fake.reset()
    await env["send"](callback_update(UID, back))
    menus = [
        c for c in fake.calls if isinstance(c, SendMessage) and getattr(c.reply_markup, "keyboard", None)
    ]
    assert menus, "back from the first screen opens the main menu"
    labels = [b.text for row in menus[-1].reply_markup.keyboard for b in row]
    assert FA("btn_buy") in labels


async def test_back_from_the_only_screen_goes_to_the_main_menu(env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_buy")))
    plans = fake.last(SendMessage)
    assert plans.text == FA("choose_plan")
    back = _back(fake, plans)
    assert back != Shop(step="start").pack(), "never back to the very same screen"
    await _press_back_to_menu(env, back)
    assert any(isinstance(c, DeleteMessage) for c in fake.calls), "the plan list is cleared away"


async def test_back_walks_up_categories_to_the_main_menu(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    cat = Category(shop_id=env["shop"].id, title="VIP")
    session.add(cat)
    await session.flush()
    env["plan"].category_id = cat.id
    await session.commit()

    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_buy")))
    cats = fake.last(SendMessage)
    assert cats.text == FA("choose_category")
    first_back = _back(fake, cats)

    await send(callback_update(UID, Shop(step="cat", loc=0, cat=cat.id).pack()))
    plans = fake.last(EditMessageText)
    assert plans.text == FA("choose_plan")
    fake.reset()
    await send(callback_update(UID, _back(fake, plans)))
    assert fake.last(EditMessageText).text == FA("choose_category"), "one step up: the categories"

    await _press_back_to_menu(env, first_back)


async def test_with_several_locations_back_returns_to_the_locations(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    de = Location(shop_id=env["shop"].id, title="DE")
    nl = Location(shop_id=env["shop"].id, title="NL")
    session.add_all([de, nl])
    await session.flush()
    env["plan"].location_id = de.id
    env["panel"].location_id = de.id
    other = await factories.plan(session, env["shop"], env["panel"], price=40_000, title="NL plan")
    other.location_id = nl.id
    await session.commit()

    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_buy")))
    locations = fake.last(SendMessage)
    assert locations.text == FA("choose_location")
    first_back = _back(fake, locations)

    await send(callback_update(UID, Shop(step="loc", loc=de.id).pack()))
    plans = fake.last(EditMessageText)
    assert plans.text == FA("choose_plan")
    fake.reset()
    await send(callback_update(UID, _back(fake, plans)))
    assert fake.last(EditMessageText).text == FA("choose_location"), "one step up: the locations"

    await _press_back_to_menu(env, first_back)


async def test_a_canceled_order_offers_the_plans_again(env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(message_update(UID, FA("btn_buy")))
    await send(callback_update(UID, Shop(step="plan", plan=env["plan"].id).pack()))
    await send(callback_update(UID, Shop(step="cancel").pack()))
    canceled = fake.last(EditMessageText)
    assert canceled.text == FA("checkout_canceled")
    again = [cb for text, cb in fake.buttons(canceled) if text == FA("btn_see_plans")]
    assert again == [Shop(step="start").pack()], "not a dead end: the plans are one press away"
    await send(callback_update(UID, again[0]))
    assert fake.last(EditMessageText).text == FA("choose_plan")
