"""Asking Telegram about a channel before it is saved as a forced or trust channel (GRW-07/08).

The admin panel runs in the API process, so it speaks to Telegram with the shop's own bot (the
main one, or a reseller's). A channel is only accepted when the bot is an admin there: otherwise
membership checks and report posts would fail later, silently and long after the mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from fastapi import HTTPException

from paurubot.core.channels import is_admin
from paurubot.notifications import telegram

CHAT_ID = re.compile(r"-?\d{5,20}")


@dataclass(slots=True)
class ResolvedChannel:
    chat_id: int
    title: str
    url: str
    bot_admin: bool


def _target(raw: str) -> str | int:
    """`@name`, a t.me link or a numeric id as Telegram expects it."""
    raw = raw.strip()
    if raw.startswith(("https://t.me/", "http://t.me/", "tg://")):
        raw = raw.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if CHAT_ID.fullmatch(raw):
        return int(raw)
    return raw


async def resolve_channel(shop_id: int, raw: str) -> ResolvedChannel:
    """Look a chat up with the shop's bot and report whether the bot may run checks there.

    422 when Telegram does not know the chat, 503 when the shop has no running bot to ask with.
    """
    bot = await telegram.bot_for(shop_id)
    if bot is None:
        raise HTTPException(status_code=503, detail="bot_stopped")
    try:
        chat = await bot.get_chat(_target(raw))
    except TelegramAPIError:
        raise HTTPException(status_code=422, detail="channel_not_found") from None
    me = await bot.me()
    bot_admin = False
    try:
        member = await bot.get_chat_member(chat.id, me.id if me else 0)
        bot_admin = is_admin(member)
    except TelegramAPIError:
        bot_admin = False
    return ResolvedChannel(
        chat_id=int(chat.id),
        title=(chat.title or "")[:96],
        url=f"https://t.me/{chat.username}" if chat.username else "",
        bot_admin=bot_admin,
    )


async def require_channel(shop_id: int, raw: str) -> ResolvedChannel:
    """Resolve a channel and refuse it unless the shop's bot is an admin there."""
    info = await resolve_channel(shop_id, raw)
    if not info.bot_admin:
        raise HTTPException(status_code=422, detail="bot_not_admin")
    return info
