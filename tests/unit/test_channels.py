"""Forced-join and trust channels: membership checks, the Mini App gate and Telegram lookups."""

from __future__ import annotations

import pytest
from aiogram.types import Chat
from fastapi import HTTPException

from paurubot import kv
from paurubot.api import webapp_auth
from paurubot.api.channels import require_channel, resolve_channel
from paurubot.db.models import ForcedChannel
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import BOT_USER, chat_key, make_bot


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    yield {"shop": shop, "cust": cust, "tg": fake}
    notify_telegram.set_bot(None)


async def test_resolve_channel_reads_telegram_and_the_bot_rights(env) -> None:  # type: ignore[no-untyped-def]
    fake = env["tg"]
    chat = Chat(id=-1009999001, type="channel", title="Pauru News", username="pauru_news")
    fake.chats[chat_key("@pauru_news")] = chat
    fake.chats["-1009999001"] = chat  # the numeric id finds the same channel
    fake.admins.add(("-1009999001", BOT_USER.id))
    info = await resolve_channel(env["shop"].id, "https://t.me/pauru_news/")
    assert info.chat_id == -1009999001
    assert info.title == "Pauru News" and info.url == "https://t.me/pauru_news"
    assert info.bot_admin is True
    assert await require_channel(env["shop"].id, "-1009999001") == info


async def test_unknown_chat_and_a_promoted_bot_are_told_apart(env, session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(HTTPException) as missing:
        await resolve_channel(env["shop"].id, "@nobody_home")
    assert missing.value.status_code == 422 and missing.value.detail == "channel_not_found"
    fake = env["tg"]
    fake.chats[chat_key("@pauru_news")] = Chat(
        id=-1009999001, type="channel", title="Pauru News", username="pauru_news"
    )
    with pytest.raises(HTTPException) as refused:  # the bot is a plain member there
        await require_channel(env["shop"].id, "@pauru_news")
    assert refused.value.status_code == 422 and refused.value.detail == "bot_not_admin"


async def test_the_mini_app_gate_answers_from_the_cache(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust = env["shop"], env["cust"]
    session.add(ForcedChannel(shop_id=shop.id, chat_id="-1009999002", title="News", url="https://t.me/news"))
    session.add(
        ForcedChannel(
            shop_id=shop.id, chat_id="-1009999005", title="Paused", url="https://t.me/paused", is_active=False
        )
    )
    await session.commit()
    fake = env["tg"]
    fake.non_members.add(("-1009999002", cust.telegram_id))
    missing = await webapp_auth.forced_gate(session, shop, cust)
    assert [row.chat_id for row in missing] == ["-1009999002"]
    fake.non_members.clear()
    await webapp_auth.forget_join_cache(shop.id, cust.telegram_id)
    assert await webapp_auth.forced_gate(session, shop, cust) == []
    calls = len(fake.calls)
    assert await webapp_auth.forced_gate(session, shop, cust) == []
    assert len(fake.calls) == calls  # the verdict is cached for a few minutes


async def test_a_channel_telegram_cannot_check_blocks_nobody(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust = env["shop"], env["cust"]
    session.add(
        ForcedChannel(shop_id=shop.id, chat_id="-1009999003", title="Broken", url="https://t.me/broken")
    )
    session.add(ForcedChannel(shop_id=shop.id, chat_id="-1009999004", title="Fine", url="https://t.me/fine"))
    await session.commit()
    fake = env["tg"]
    fake.non_members.add(("-1009999004", cust.telegram_id))
    fake.unreachable.add("-1009999003")
    missing = await webapp_auth.forced_gate(session, shop, cust)
    assert [row.chat_id for row in missing] == ["-1009999004"]
    marks = await kv.marked([f"join:{shop.id}:-1009999003"])
    assert marks and "not a member" in marks[f"join:{shop.id}:-1009999003"]
