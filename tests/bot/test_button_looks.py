"""ClockBot parity: every button of the bot can have its own color and premium emoji, set once by
its label key; buttons that already carry their own look (main menu, plans) keep it."""

from __future__ import annotations

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from paurubot.bot import button_looks
from paurubot.bot.callbacks import Admin
from paurubot.core import settings as shop_settings
from tests.bot.test_admin_tools import FA, OWNER, env  # noqa: F401  (shared fixture)
from tests.fakes.telegram import callback_update, message_update

EMOJI = "5368324170671202286"


def _buttons(markup):  # type: ignore[no-untyped-def]
    rows = markup.keyboard if isinstance(markup, ReplyKeyboardMarkup) else markup.inline_keyboard
    return [b for row in rows for b in row]


async def test_any_button_gets_its_color_and_premium_emoji(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {
            "button_looks": {
                "btn_buy": {"style": "success", "emoji_id": EMOJI},
                "adm_nav_home": {"style": "danger"},
                "btn_back": {"style": "primary"},
            },
            # The main menu's own look for "services" wins over nothing at all.
            "button_styles": {"services": {"style": "danger"}},
        },
    )
    await session.commit()
    button_looks.forget()
    fake.middleware(button_looks.ButtonLookMiddleware(env["shop"].id))  # as make_bot wires it
    await send(message_update(OWNER, "/start"))
    menu = [
        c
        for c in fake.calls
        if isinstance(c, SendMessage) and isinstance(c.reply_markup, ReplyKeyboardMarkup)
    ][-1]
    by_text = {b.text: b for b in _buttons(menu.reply_markup)}
    assert by_text[FA("btn_buy")].style == "success"
    assert by_text[FA("btn_buy")].icon_custom_emoji_id == EMOJI
    assert by_text[FA("btn_services")].style == "danger"  # its own look stays
    # An admin inline button deep in a menu gets its look too.
    await send(callback_update(OWNER, Admin(section="nav", action="users").pack()))
    inline = fake.last(EditMessageText).reply_markup
    homes = [b for b in _buttons(inline) if b.text == FA("adm_nav_home")]
    assert homes and homes[0].style == "danger"


def test_looks_are_checked() -> None:
    import pytest

    from paurubot.core.settings import SettingError, _check

    for bad in (
        {"no_such_key": {"style": "success"}},
        {"btn_buy": {"style": "purple"}},
        {"btn_buy": {"emoji_id": "abc"}},
    ):
        with pytest.raises(SettingError):
            _check("button_looks", bad)
    _check("button_looks", {"btn_buy": {"style": "", "emoji_id": EMOJI}})
    assert "btn_buy" in button_looks.label_keys() and "adm_nav_home" in button_looks.label_keys()
    assert isinstance(InlineKeyboardMarkup(inline_keyboard=[]), InlineKeyboardMarkup)
