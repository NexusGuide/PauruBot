"""Telegram delivery with error classification (SVC-10)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Message

from paurubot.config import get_settings
from paurubot.worker.runner import Retry

log = logging.getLogger(__name__)

_bot: Bot | None = None


def make_bot(token: str | None = None, shop_id: int | None = None) -> Bot:
    from paurubot.bot.polling import PollingRetryMiddleware
    from paurubot.bot.premium_emoji import PremiumEmojiMiddleware

    bot = Bot(
        token or get_settings().bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    bot.session.middleware(PollingRetryMiddleware(shop_id))
    bot.session.middleware(PremiumEmojiMiddleware(shop_id))  # BOT-10
    return bot


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = make_bot()
    return _bot


def set_bot(bot: Bot | None) -> None:
    """Install one bot for every shop (tests); None goes back to building them from tokens."""
    global _bot, _single
    _bot = bot
    _single = bot is not None
    _shop_bots.clear()


# A reseller's own bot (TEN-04) talks to the customers of its shop; everyone else hears the main bot.
_single = False
_shop_bots: dict[int, tuple[float, Bot | None]] = {}
SHOP_BOT_TTL = 120.0


def bot_id_of(token: str) -> int | None:
    head = token.split(":", 1)[0]
    return int(head) if head.isdigit() else None


async def bot_for(shop_id: int | None) -> Bot | None:
    """The bot that speaks for a shop. None for a reseller's shop whose bot is stopped: its
    customers must not suddenly hear from the main bot."""
    if not shop_id or _single:
        return get_bot()
    cached = _shop_bots.get(shop_id)
    if cached is not None and time.monotonic() - cached[0] < SHOP_BOT_TTL:
        return cached[1]
    from paurubot.db.models import Shop
    from paurubot.db.session import session_scope
    from paurubot.security import secretbox

    bot: Bot | None = None
    async with session_scope() as session:
        shop = await session.get(Shop, shop_id)
        if shop is None or shop.reseller_profile_id is None:
            bot = get_bot()
        elif shop.bot_token_enc:
            bot = make_bot(secretbox.decrypt("bot-token", shop.bot_token_enc), shop_id)
    old = _shop_bots.get(shop_id)
    _shop_bots[shop_id] = (time.monotonic(), bot)
    if old and old[1] is not None and old[1] is not bot and old[1] is not _bot:
        await old[1].session.close()
    return bot


def forget_shop_bot(shop_id: int) -> None:
    _shop_bots.pop(shop_id, None)


class Undeliverable(Exception):
    """The chat cannot receive messages (blocked bot, deleted account); never retried."""


async def deliver[R](call: Callable[[], Awaitable[R]]) -> R:
    try:
        return await call()
    except TelegramForbiddenError as exc:
        raise Undeliverable(str(exc)) from exc
    except TelegramBadRequest as exc:
        text = str(exc).lower()
        if "chat not found" in text or "user is deactivated" in text or "peer_id_invalid" in text:
            raise Undeliverable(str(exc)) from exc
        raise
    except TelegramRetryAfter as exc:
        raise Retry(f"flood control, retry after {exc.retry_after}s") from exc
    except (TelegramNetworkError, TelegramServerError) as exc:
        raise Retry(f"telegram unavailable: {exc.__class__.__name__}") from exc


GUEST_CHAT_LIMIT = -(10**15)  # web guests (CAT-08) have placeholder ids at or below this


def is_guest_chat(chat_id: int) -> bool:
    """Telegram channel ids stay far above this; anything here is a guest with no chat at all."""
    return int(chat_id) <= GUEST_CHAT_LIMIT


async def send_text(chat_id: int, text: str, *, shop_id: int | None = None, **kwargs: Any) -> Message | None:
    if is_guest_chat(chat_id):
        return None
    bot = await bot_for(shop_id)
    if bot is None:
        return None
    return await deliver(lambda: bot.send_message(chat_id, text, **kwargs))


async def send_photo(
    chat_id: int, photo: Any, caption: str | None = None, *, shop_id: int | None = None, **kwargs: Any
) -> Message | None:
    if is_guest_chat(chat_id):
        return None
    bot = await bot_for(shop_id)
    if bot is None:
        return None
    return await deliver(lambda: bot.send_photo(chat_id, photo, caption=caption, **kwargs))


async def send_document(
    chat_id: int, document: Any, caption: str | None = None, *, shop_id: int | None = None, **kwargs: Any
) -> None:
    if is_guest_chat(chat_id):
        return
    bot = await bot_for(shop_id)
    if bot is None:
        return
    await deliver(lambda: bot.send_document(chat_id, document, caption=caption, **kwargs))
