"""A color and a premium emoji for any button of the bot (ClockBot parity).

Staff set a look per button label key (`button_looks`: {"btn_buy": {"style": "success",
"emoji_id": "53..."}}). This request middleware finds those buttons in everything the bot sends
by their label in the shop's languages and adds the look. A button that already has its own look
(the main menu's, a plan's, a category's) keeps it. If Telegram refuses premium emoji for this bot,
the buttons go out with their colors only, and the icons rest for a while.
"""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

log = logging.getLogger(__name__)

STYLES = {"primary", "success", "danger"}
CACHE_SECONDS = 60.0
REST_SECONDS = 600.0
_ID = re.compile(r"^\d{5,25}$")

_cache: dict[int | None, tuple[float, dict[str, dict[str, str]]]] = {}
_icons_resting_until: dict[int, float] = {}


@lru_cache(maxsize=1)
def label_keys() -> tuple[str, ...]:
    """Every text that is a button label: menu buttons, feature buttons and the admin menus."""
    from paurubot.bot.routers.admin_navigation import GROUPS
    from paurubot.i18n import catalog

    keys = set()
    for key in catalog("fa"):
        if key.startswith("btn_") or "_btn_" in key:
            keys.add(key)
        elif key.startswith("adm_nav_") and not key.endswith(("_text", "_no_web")) and key != "adm_nav_open":
            keys.add(key)
    keys |= {entry.key for entries in GROUPS.values() for entry in entries}
    return tuple(sorted(k for k in keys if k in catalog("fa")))


def clean(value: Any) -> dict[str, dict[str, str]]:
    """Known label keys with an allowed color and/or a numeric premium emoji id; else an error."""
    from paurubot.core.settings import SettingError

    if not isinstance(value, dict) or len(value) > 1000:
        raise SettingError("button_looks_invalid")
    known = set(label_keys())
    out: dict[str, dict[str, str]] = {}
    for key, look in value.items():
        if key not in known or not isinstance(look, dict):
            raise SettingError("button_looks_invalid")
        style, emoji = str(look.get("style") or ""), str(look.get("emoji_id") or "")
        if (style and style not in STYLES) or (emoji and not _ID.match(emoji)):
            raise SettingError("button_looks_invalid")
        if style or emoji:
            out[key] = {k: v for k, v in (("style", style), ("emoji_id", emoji)) if v}
    return out


async def looks_for(shop_id: int | None) -> dict[str, dict[str, str]]:
    """Label text (in each of the shop's languages) → look, cached for a minute."""
    cached = _cache.get(shop_id)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    from paurubot.core import settings as shop_settings
    from paurubot.core.shops import root_shop
    from paurubot.db.models import Shop
    from paurubot.db.session import session_scope
    from paurubot.i18n import LOCALES, Texts, load_overrides

    by_text: dict[str, dict[str, str]] = {}
    try:
        async with session_scope() as session:
            shop = await session.get(Shop, shop_id) if shop_id is not None else await root_shop(session)
            target = shop.id if shop else None
            if target is not None:
                cfg = await shop_settings.load(session, target)
                looks = cfg.get("button_looks") or {}
                if looks:
                    overrides = await load_overrides(session, target)
                    context = {
                        "shop": str(cfg.get("shop_name") or (shop.title if shop else "") or ""),
                        "support": str(cfg.get("support_username") or ""),
                    }
                    for locale in LOCALES:
                        t = Texts(locale, overrides, context)
                        for key, look in looks.items():
                            label = t(key)
                            if label and label != key and isinstance(look, dict):
                                by_text.setdefault(label, look)
    except Exception:  # no database yet (setup) or a hiccup: do not poison the cache
        log.debug("button looks unavailable", exc_info=True)
        return by_text
    _cache[shop_id] = (time.monotonic(), by_text)
    return by_text


def forget(shop_id: int | None = None) -> None:
    if shop_id is None:
        _cache.clear()
    else:
        _cache.pop(shop_id, None)
        _cache.pop(None, None)


def restyle(markup: Any, looks: dict[str, dict[str, str]], *, icons: bool = True) -> Any:
    """The same keyboard with the looks added to buttons that have none of their own."""
    if not looks or not isinstance(markup, InlineKeyboardMarkup | ReplyKeyboardMarkup):
        return markup
    inline = isinstance(markup, InlineKeyboardMarkup)
    rows: list[list[Any]] = (
        markup.inline_keyboard if isinstance(markup, InlineKeyboardMarkup) else markup.keyboard
    )
    changed = False
    new_rows = []
    for row in rows:
        new_row = []
        for button in row:
            look = looks.get(getattr(button, "text", "") or "")
            own = getattr(button, "style", None) or getattr(button, "icon_custom_emoji_id", None)
            # A Mini App button with an icon makes some clients misbehave (as ClockBot found).
            web_app = getattr(button, "web_app", None) is not None
            if look and not own:
                update: dict[str, Any] = {}
                if look.get("style") in STYLES:
                    update["style"] = look["style"]
                if icons and look.get("emoji_id") and not web_app:
                    update["icon_custom_emoji_id"] = look["emoji_id"]
                if update:
                    button = button.model_copy(update=update)
                    changed = True
            new_row.append(button)
        new_rows.append(new_row)
    if not changed:
        return markup
    field = "inline_keyboard" if inline else "keyboard"
    return markup.model_copy(update={field: new_rows})


def _strip_icons(markup: Any) -> Any:
    if not isinstance(markup, InlineKeyboardMarkup | ReplyKeyboardMarkup):
        return markup
    inline = isinstance(markup, InlineKeyboardMarkup)
    rows: list[list[Any]] = (
        markup.inline_keyboard if isinstance(markup, InlineKeyboardMarkup) else markup.keyboard
    )
    plain = [[b.model_copy(update={"icon_custom_emoji_id": None}) for b in row] for row in rows]
    return markup.model_copy(update={"inline_keyboard" if inline else "keyboard": plain})


class ButtonLookMiddleware(BaseRequestMiddleware):
    def __init__(self, shop_id: int | None = None) -> None:
        self.shop_id = shop_id

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        markup = getattr(method, "reply_markup", None)
        if not isinstance(markup, InlineKeyboardMarkup | ReplyKeyboardMarkup):
            return await make_request(bot, method)
        looks = await looks_for(self.shop_id)
        icons = _icons_resting_until.get(bot.id, 0.0) <= time.monotonic()
        styled = restyle(markup, looks, icons=icons)
        if styled is markup:
            return await make_request(bot, method)
        method.reply_markup = styled  # type: ignore[attr-defined]
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if "emoji" not in str(exc).lower():
                raise
            log.warning("premium button icons refused for bot %s; colors only for a while", bot.id)
            _icons_resting_until[bot.id] = time.monotonic() + REST_SECONDS
            method.reply_markup = _strip_icons(styled)  # type: ignore[attr-defined]
            return await make_request(bot, method)
