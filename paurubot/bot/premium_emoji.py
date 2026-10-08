"""Premium (custom) emoji in every message the bot sends (BOT-10).

Staff map ordinary emoji to custom emoji once; this request middleware wraps each mapped emoji in
`<tg-emoji>` on the way out. Text inside <code>, <pre> and existing <tg-emoji> is left alone. If
Telegram refuses custom emoji for this bot (it needs a Fragment username), the message goes out
unchanged and the feature rests for a while instead of breaking delivery.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    CopyMessage,
    EditMessageCaption,
    EditMessageText,
    SendAnimation,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
REST_SECONDS = 600.0
_SKIP = re.compile(r"(<code>.*?</code>|<pre>.*?</pre>|<tg-emoji[^>]*>.*?</tg-emoji>|<[^>]+>)", re.S)
_ID = re.compile(r"^\d{5,25}$")

_cache: dict[int | None, tuple[float, dict[str, str]]] = {}
_resting_until: dict[int, float] = {}


def clean_map(value: Any) -> dict[str, str]:
    """Only short emoji keys and numeric custom-emoji ids survive."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for emoji, custom_id in value.items():
        emoji, custom_id = str(emoji), str(custom_id)
        if 0 < len(emoji) <= 8 and _ID.match(custom_id) and not any(ch in emoji for ch in "<>&\"'"):
            out[emoji] = custom_id
    return out


def apply(text: str, mapping: dict[str, str]) -> str:
    if not mapping or not text:
        return text
    pattern = re.compile("|".join(re.escape(e) for e in sorted(mapping, key=len, reverse=True)))

    def swap(part: str) -> str:
        return pattern.sub(
            lambda m: f'<tg-emoji emoji-id="{mapping[m.group(0)]}">{html.escape(m.group(0))}</tg-emoji>', part
        )

    pieces = _SKIP.split(text)
    return "".join(piece if _SKIP.fullmatch(piece) else swap(piece) for piece in pieces)


async def mapping_for(shop_id: int | None) -> dict[str, str]:
    cached = _cache.get(shop_id)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    from paurubot.core import settings as shop_settings
    from paurubot.core.shops import root_shop
    from paurubot.db.session import session_scope

    mapping: dict[str, str] = {}
    try:
        async with session_scope() as session:
            target = shop_id
            if target is None:
                shop = await root_shop(session)
                target = shop.id if shop else None
            if target is not None:
                cfg = await shop_settings.load(session, target)
                if cfg.get("premium_emoji_enabled"):
                    mapping = clean_map(cfg.get("premium_emojis"))
    except Exception:  # no database yet (setup) or a hiccup: plain emoji
        log.debug("premium emoji map unavailable", exc_info=True)
    _cache[shop_id] = (time.monotonic(), mapping)
    return mapping


def forget(shop_id: int | None = None) -> None:
    if shop_id is None:
        _cache.clear()
    else:
        _cache.pop(shop_id, None)
        _cache.pop(None, None)


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    def __init__(self, shop_id: int | None = None) -> None:
        self.shop_id = shop_id

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        field = (
            "text"
            if isinstance(method, (SendMessage, EditMessageText))
            else "caption"
            if isinstance(
                method, (SendPhoto, SendDocument, SendVideo, SendAnimation, EditMessageCaption, CopyMessage)
            )
            else None
        )
        original = getattr(method, field, None) if field else None
        if (
            not isinstance(original, str)
            or not original
            or bot.id in _resting_until
            and _resting_until[bot.id] > time.monotonic()
        ):
            return await make_request(bot, method)
        mode = getattr(method, "parse_mode", None)
        if isinstance(mode, str) and mode.upper() != "HTML":
            return await make_request(bot, method)
        mapping = await mapping_for(self.shop_id)
        changed = apply(original, mapping)
        if changed == original:
            return await make_request(bot, method)
        setattr(method, field, changed)  # type: ignore[arg-type]
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if "emoji" not in str(exc).lower() and "entit" not in str(exc).lower():
                raise
            log.warning("custom emoji refused for bot %s; sending plain emoji for a while", bot.id)
            _resting_until[bot.id] = time.monotonic() + REST_SECONDS
            setattr(method, field, original)  # type: ignore[arg-type]
            return await make_request(bot, method)
