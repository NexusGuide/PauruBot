"""The Mini App button is called «مینی اپ» / "Mini App"; an old keyboard's label still works."""

from __future__ import annotations

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

from paurubot.bot.app import build_dispatcher
from paurubot.config import get_settings
from paurubot.i18n import FORMER_LABELS, Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import make_bot, message_update

FA = Texts("fa")


async def test_new_and_old_labels_open_the_mini_app(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    shop = await factories.shop(session, "Labels")
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)
    try:
        assert "مینی اپ" in FA("btn_miniapp") and "پائورو" not in FA("btn_miniapp")
        for label in (FA("btn_miniapp"), *FORMER_LABELS["btn_miniapp"]):
            await dp.feed_update(bot, message_update(7801, label))
            assert fake.last(SendMessage).text == FA("miniapp_open"), label
    finally:
        notify_telegram.set_bot(None)
