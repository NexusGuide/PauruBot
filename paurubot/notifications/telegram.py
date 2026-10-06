"""Telegram delivery with error classification (SVC-10)."""

from __future__ import annotations

import logging
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

from paurubot.config import get_settings
from paurubot.worker.runner import Retry

log = logging.getLogger(__name__)

_bot: Bot | None = None


def make_bot(token: str | None = None) -> Bot:
    return Bot(
        token or get_settings().bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = make_bot()
    return _bot


def set_bot(bot: Bot | None) -> None:
    global _bot
    _bot = bot


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


async def send_text(chat_id: int, text: str, **kwargs: Any) -> None:
    bot = get_bot()
    await deliver(lambda: bot.send_message(chat_id, text, **kwargs))


async def send_photo(chat_id: int, photo: Any, caption: str | None = None, **kwargs: Any) -> None:
    bot = get_bot()
    await deliver(lambda: bot.send_photo(chat_id, photo, caption=caption, **kwargs))
