"""Forced-join channels of any shop, the main one or a user's own bot (GRW-07).

The admin panel, a dedicated bot's Mini App and a reseller's bot all add channels through here, so
the same rule holds everywhere: Telegram must know the chat and the shop's *own* bot must be an
admin there. Otherwise the join check can never run (core/channels.py lets everyone through when
Telegram cannot answer) and the owner would believe customers are held when nobody is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.channels import is_admin
from paurubot.core.shops import lock_shop
from paurubot.db.models import ForcedChannel

MAX_CHANNELS = 5
CHAT_ID = re.compile(r"-?\d{5,20}")
INVITE = re.compile(r"https://t\.me/[A-Za-z0-9_+/=-]{4,}")


class ForcedJoinError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class ResolvedChannel:
    chat_id: int
    title: str
    url: str
    bot_admin: bool


def target(raw: str) -> str | int:
    """`@name`, a t.me link or a numeric id as Telegram expects it."""
    raw = raw.strip()
    if raw.startswith(("https://t.me/", "http://t.me/", "tg://")):
        raw = raw.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if CHAT_ID.fullmatch(raw):
        return int(raw)
    if raw and not raw.startswith("@") and re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        return "@" + raw
    return raw


async def resolve(shop_id: int, raw: str) -> ResolvedChannel:
    """Look a chat up with the shop's own bot and say whether that bot may run checks there."""
    from paurubot.notifications import telegram

    bot = await telegram.bot_for(shop_id)
    if bot is None:
        raise ForcedJoinError("bot_stopped")
    try:
        chat = await bot.get_chat(target(raw))
    except TelegramAPIError:
        raise ForcedJoinError("channel_not_found") from None
    me = await bot.me()
    bot_admin = False
    try:
        bot_admin = is_admin(await bot.get_chat_member(chat.id, me.id if me else 0))
    except TelegramAPIError:
        bot_admin = False
    return ResolvedChannel(
        chat_id=int(chat.id),
        title=(chat.title or "")[:96],
        url=f"https://t.me/{chat.username}" if chat.username else "",
        bot_admin=bot_admin,
    )


def split_input(raw: str) -> tuple[str, str]:
    """The channel, and an invite link when one was given after it (`@channel | https://t.me/+x`)."""
    channel, _, link = (part.strip() for part in raw.partition("|"))
    if not link and INVITE.fullmatch(channel) and "/+" in channel:
        return channel, channel  # a private invite link alone: Telegram cannot look it up
    return channel, link if INVITE.fullmatch(link) else ""


async def add(session: AsyncSession, shop_id: int, raw: str, *, title: str = "") -> ForcedChannel:
    """Add a channel the shop's customers must join; ForcedJoinError explains a refusal."""
    channel, link = split_input(raw)
    if not channel:
        raise ForcedJoinError("channel_not_found")
    await lock_shop(session, shop_id)  # two adds at once can't both take the last slot
    count = await session.scalar(select(func.count(ForcedChannel.id)).where(ForcedChannel.shop_id == shop_id))
    if (count or 0) >= MAX_CHANNELS:
        raise ForcedJoinError("channel_limit")
    info = await resolve(shop_id, channel)
    if not info.bot_admin:
        raise ForcedJoinError("bot_not_admin")
    chat_id = str(info.chat_id)
    exists = await session.scalar(
        select(ForcedChannel.id).where(ForcedChannel.shop_id == shop_id, ForcedChannel.chat_id == chat_id)
    )
    if exists:
        raise ForcedJoinError("channel_exists")
    url = link or info.url
    if not url:
        raise ForcedJoinError("link_required")  # a private channel needs its invite link
    row = ForcedChannel(
        shop_id=shop_id, chat_id=chat_id, title=(title.strip() or info.title or chat_id)[:96], url=url[:255]
    )
    session.add(row)
    await session.flush()
    return row


async def listing(session: AsyncSession, shop_id: int) -> list[ForcedChannel]:
    return list(
        await session.scalars(
            select(ForcedChannel)
            .where(ForcedChannel.shop_id == shop_id)
            .order_by(ForcedChannel.sort, ForcedChannel.id)
        )
    )


async def owned(session: AsyncSession, shop_id: int, row_id: int) -> ForcedChannel:
    row = await session.get(ForcedChannel, row_id)
    if row is None or row.shop_id != shop_id:
        raise ForcedJoinError("channel_not_found")
    return row


def view(row: ForcedChannel) -> dict[str, object]:
    return {"id": row.id, "title": row.title, "url": row.url, "is_active": row.is_active}
