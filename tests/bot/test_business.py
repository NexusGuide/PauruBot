"""Telegram Business automation (BOT-07)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import BusinessBotRights, BusinessConnection, Chat, Message, Update, User
from sqlalchemy import func, select

from paurubot import kv
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.core import business
from paurubot.core import settings as shop_settings
from paurubot.db.models import BusinessConnection as Row
from paurubot.db.models import Customer, StaffRole
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import make_bot

OWNER = 7700
_ids = itertools.count(50_000)


def connection_update(conn_id: str, owner: int, *, can_reply: bool = True, enabled: bool = True) -> Update:
    conn = BusinessConnection(
        id=conn_id,
        user=User(id=owner, is_bot=False, first_name="Owner"),
        user_chat_id=owner,
        date=datetime.now(UTC),
        is_enabled=enabled,
        rights=BusinessBotRights(can_reply=can_reply),
    )
    return Update(update_id=next(_ids), business_connection=conn)


def business_message(conn_id: str, chat_id: int, sender: int, text: str) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=sender, is_bot=False, first_name="Someone"),
        text=text,
        business_connection_id=conn_id,
    )
    return Update(update_id=next(_ids), business_message=msg)


def test_rules_validation_and_matching() -> None:
    rules = business.validate_rules(
        [
            {"keywords": "Price, قیمت", "reply": "See the price list"},
            {"keywords": ["test"], "reply": "Trial!"},
        ]
    )
    assert rules[0]["keywords"] == ["price", "قیمت"]
    assert business.match(rules, "what's the PRICE?") == "See the price list"
    assert business.match(rules, "free test please") == "Trial!"
    assert business.match(rules, "hello") is None
    for bad in (
        [{"keywords": "", "reply": "x"}],
        [{"keywords": "a", "reply": ""}],
        "nope",
        [{"reply": "x" * 2000, "keywords": "a"}],
    ):
        with pytest.raises(business.BusinessError):
            business.validate_rules(bad)


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Biz shop")
    shop.bot_username = "pauru_test_bot"
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="biz_owner", password=None, telegram_id=OWNER
    )
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "business_enabled": True,
            "business_reply": "Hi! Order any time at {bot}",
            "business_rules": [{"keywords": ["price"], "reply": "Plans start at 50,000 Toman."}],
        },
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)
    redis = kv.get_redis()
    for key in await redis.keys("*biz*"):
        await redis.delete(key)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


def _replies(fake, conn_id: str) -> list[SendMessage]:  # type: ignore[no-untyped-def]
    return [c for c in fake.calls if isinstance(c, SendMessage) and c.business_connection_id == conn_id]


async def test_staff_account_gets_answers_without_new_customers(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    before = await session.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id))
    await send(connection_update("bc-1", OWNER))
    row = await session.get(Row, "bc-1")
    assert row is not None and row.trusted and row.can_reply
    assert fake.last(SendMessage).chat_id == OWNER  # the owner hears it is connected

    await send(business_message("bc-1", 9001, 9001, "hello there"))
    [standard] = _replies(fake, "bc-1")
    assert standard.text == "Hi! Order any time at https://t.me/pauru_test_bot" and standard.chat_id == 9001
    await send(business_message("bc-1", 9001, 9001, "anyone?"))
    assert len(_replies(fake, "bc-1")) == 1  # the standard reply goes once per period
    await send(business_message("bc-1", 9001, 9001, "what is the price"))
    assert _replies(fake, "bc-1")[-1].text == "Plans start at 50,000 Toman."

    await send(business_message("bc-1", 9002, OWNER, "let me check for you"))  # the owner writes
    await send(business_message("bc-1", 9002, 9002, "price?"))
    assert all(r.chat_id != 9002 for r in _replies(fake, "bc-1"))  # quiet while the owner talks
    after = await session.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id))
    assert after == before


async def test_strangers_and_missing_rights_are_ignored(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(connection_update("bc-stranger", 123456789))
    row = await session.get(Row, "bc-stranger")
    assert row is not None and not row.trusted
    await send(business_message("bc-stranger", 9003, 9003, "price"))
    assert _replies(fake, "bc-stranger") == []

    await send(connection_update("bc-mute", OWNER, can_reply=False))
    await send(business_message("bc-mute", 9004, 9004, "price"))
    assert _replies(fake, "bc-mute") == []
    await send(connection_update("bc-1", OWNER, enabled=False))
    await send(business_message("bc-1", 9005, 9005, "price"))
    assert _replies(fake, "bc-1") == []
