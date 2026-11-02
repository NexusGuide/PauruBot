"""One home menu per chat: a new home menu deletes the bot's previous one (P3, after PGClockBot).

Every /start, "back" and finished flow sent a fresh menu and left the old ones in the chat, each
with buttons that still looked live. Only the message the bot sent as the home menu is deleted.
"""

from __future__ import annotations

from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat

from paurubot.bot.routers import start
from paurubot.i18n import Texts
from tests.unit.test_bot_menu_modes import cfg_for, incoming, redis_clean, telegram

__all__ = ["redis_clean", "telegram"]


def _deleted(requests) -> list[int]:  # type: ignore[no-untyped-def]
    return [r.message_id for r in requests if isinstance(r, DeleteMessage)]


def _sent(requests) -> list[int]:  # type: ignore[no-untyped-def]
    return [i + 1 for i, r in enumerate(requests) if isinstance(r, SendMessage)]


async def test_a_new_home_menu_deletes_the_previous_one(telegram, redis_clean) -> None:  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t, cfg = Texts("en"), cfg_for("reply")
    await start.home(incoming(bot), t, cfg, None)
    first = _sent(requests)[-1]
    assert _deleted(requests) == []  # nothing before the first menu
    await start.home(incoming(bot), t, cfg, None)
    second = _sent(requests)[-1]
    assert _deleted(requests) == [first]
    await start.home(incoming(bot), t, cfg, None)
    assert _deleted(requests) == [first, second]  # always just the one before


async def test_the_shop_can_keep_every_menu(telegram, redis_clean) -> None:  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t, cfg = Texts("en"), {**cfg_for("reply"), "menu_cleanup": False}
    for _ in range(3):
        await start.home(incoming(bot), t, cfg, None)
    assert _deleted(requests) == []


async def test_inline_mode_only_adds_its_own_keyboard_cleanup(telegram, redis_clean) -> None:  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t, cfg = Texts("en"), cfg_for("inline")
    await start.home(incoming(bot), t, cfg, None)
    first_menu = _sent(requests)[-1]
    await start.home(incoming(bot), t, cfg, None)
    # The "⌨️" placeholder of each call is deleted as before; the old menu once.
    assert _deleted(requests).count(first_menu) == 1


async def test_a_failed_delete_never_costs_the_menu(telegram, redis_clean, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t, cfg = Texts("en"), cfg_for("reply")
    await start.home(incoming(bot), t, cfg, None)

    async def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("message to delete not found")

    monkeypatch.setattr(type(bot), "delete_message", refuse)
    await start.home(incoming(bot), t, cfg, None)
    assert len(_sent(requests)) == 2


async def test_redis_trouble_never_costs_the_menu(telegram, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot import kv

    async def down(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("redis down")

    monkeypatch.setattr(kv, "marked", down)
    bot, requests = telegram
    await start.home(incoming(bot), Texts("en"), cfg_for("reply"), None)
    assert len(_sent(requests)) == 1 and _deleted(requests) == []


async def test_group_chats_are_left_alone(telegram, redis_clean) -> None:  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t, cfg = Texts("en"), cfg_for("reply")
    group = incoming(bot).model_copy(update={"chat": Chat(id=-100, type="supergroup")})
    for _ in range(2):
        await start.home(group.as_(bot), t, cfg, None)
    assert _deleted(requests) == []
