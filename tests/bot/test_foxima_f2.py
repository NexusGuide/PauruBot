"""Foxima parity, batch F2 in the bot: buying by duration first. Off by default."""

from __future__ import annotations

from aiogram.methods import EditMessageText, SendMessage

from paurubot.bot.callbacks import Shop
from paurubot.core import settings as shop_settings
from tests import factories
from tests.bot.test_flows import FA, UID, env  # noqa: F401  (shared fixture)
from tests.fakes.telegram import callback_update, message_update


async def test_buying_by_duration_first(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    panel_id = env["plan"].panel_id
    from paurubot.db.models import Panel

    panel = await session.get(Panel, panel_id)
    quarterly = await factories.plan(session, env["shop"], panel, price=270_000, title="Quarterly", days=90)
    await send(message_update(UID, "/start"))
    # Off: the plans as before.
    await send(message_update(UID, FA("btn_buy")))
    titles = " ".join(text for text, _ in fake.buttons(fake.last(SendMessage)))
    assert "Monthly" in titles and "Quarterly" in titles
    await shop_settings.set_many(session, env["shop"].id, {"buy_by_duration": True})
    await session.commit()
    await send(message_update(UID, FA("btn_buy")))
    durations = [text for text, _ in fake.buttons(fake.last(SendMessage))]
    assert FA("duration_option", days=30) in durations and FA("duration_option", days=90) in durations
    await send(callback_update(UID, Shop(step="dur", n=90, kind="all").pack()))
    plans = " ".join(text for text, _ in fake.buttons(fake.last(EditMessageText)))
    assert "Quarterly" in plans and "Monthly" not in plans
    assert quarterly.days == 90
