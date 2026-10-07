"""Telegram Business updates: connections and messages to the owner's own account (BOT-07)."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection as TgBusinessConnection
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.context import resolve_staff
from paurubot.config import get_settings
from paurubot.core import business
from paurubot.db.models import BusinessConnection, Shop
from paurubot.i18n import Texts

log = logging.getLogger(__name__)
router = Router(name="business")


def _can_reply(connection: TgBusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


@router.business_connection()
async def connected(
    connection: TgBusinessConnection,
    bot: Bot,
    session: AsyncSession,
    shop: Shop,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    owner = connection.user.id
    trusted = (
        owner in get_settings().owner_telegram_ids or await resolve_staff(session, shop.id, owner) is not None
    )
    await business.remember(
        session,
        shop.id,
        connection.id,
        owner,
        enabled=connection.is_enabled,
        can_reply=_can_reply(connection),
        trusted=trusted,
    )
    if not connection.is_enabled:
        return
    if not trusted:
        log.info("business connection from a non-staff account %s ignored", owner)
        return
    key = "business_connected" if cfg.get("business_enabled") else "business_connected_off"
    if not _can_reply(connection):
        key = "business_connected_no_reply"
    try:
        await bot.send_message(connection.user_chat_id, t(key))
    except TelegramAPIError:
        pass


@router.business_message()
async def incoming(
    message: Message, bot: Bot, session: AsyncSession, shop: Shop, cfg: dict[str, Any]
) -> None:
    if not message.business_connection_id or message.from_user is None:
        return
    row = await session.get(BusinessConnection, message.business_connection_id)
    if row is None or row.shop_id != shop.id:
        return
    if message.from_user.id == row.user_id:
        await business.owner_spoke(row.id, message.chat.id, int(cfg.get("business_quiet_minutes") or 0))
        return
    if message.from_user.is_bot:
        return
    text = await business.reply_for(
        cfg, row, message.chat.id, message.text or message.caption or "", shop.bot_username
    )
    if not text:
        return
    try:
        await bot.send_message(
            message.chat.id,
            text,
            business_connection_id=row.id,
            reply_to_message_id=message.message_id,
            parse_mode=None,
        )
    except TelegramAPIError as exc:
        log.info("business reply failed on %s: %s", row.id, exc)
        return
    row.replies = int(row.replies or 0) + 1
