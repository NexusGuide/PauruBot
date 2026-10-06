"""The main reply keyboard: shop-defined layout (BOT-02) and button colours / custom emoji (BOT-03)."""

from __future__ import annotations

import re
from typing import Any

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from paurubot.config import get_settings
from paurubot.i18n import Texts

# Button id → locale key of its label.
BUTTONS: dict[str, str] = {
    "miniapp": "btn_miniapp",
    "buy": "btn_buy",
    "services": "btn_services",
    "wallet": "btn_wallet",
    "trial": "btn_trial",
    "referral": "btn_referral",
    "reseller": "btn_reseller",
    "tickets": "btn_tickets",
    "guide": "btn_guide",
    "apps": "btn_apps",
    "language": "btn_language",
}

DEFAULT_LAYOUT: list[list[str]] = [
    ["miniapp"],
    ["buy", "services"],
    ["wallet", "trial"],
    ["referral", "reseller"],
    ["tickets", "guide"],
    ["apps", "language"],
]

STYLES = {"primary", "success", "danger"}
_EMOJI_ID = re.compile(r"^\d{5,25}$")
MAX_ROWS = 8
MAX_PER_ROW = 4


class LayoutError(ValueError):
    pass


def validate_layout(value: Any) -> list[list[str]]:
    """Rows of known button ids, each used once. `buy` and `services` can't be hidden."""
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise LayoutError("menu_layout_invalid")
    seen: set[str] = set()
    rows: list[list[str]] = []
    for row in value:
        if not isinstance(row, list) or len(row) > MAX_PER_ROW:
            raise LayoutError("menu_layout_invalid")
        clean = []
        for item in row:
            if item not in BUTTONS or item in seen:
                raise LayoutError("menu_layout_invalid")
            seen.add(item)
            clean.append(item)
        if clean:
            rows.append(clean)
    if value and not {"buy", "services"} <= seen:
        raise LayoutError("menu_layout_needs_buy")
    return rows


def validate_styles(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise LayoutError("button_styles_invalid")
    clean: dict[str, dict[str, str]] = {}
    for button, style in value.items():
        if button not in BUTTONS and button != "admin":
            raise LayoutError("button_styles_invalid")
        if not isinstance(style, dict):
            raise LayoutError("button_styles_invalid")
        entry: dict[str, str] = {}
        if style.get("style"):
            if style["style"] not in STYLES:
                raise LayoutError("button_styles_invalid")
            entry["style"] = str(style["style"])
        if style.get("emoji_id"):
            if not _EMOJI_ID.match(str(style["emoji_id"])):
                raise LayoutError("button_emoji_invalid")
            entry["emoji_id"] = str(style["emoji_id"])
        if entry:
            clean[button] = entry
    return clean


def styled(text: str, style: dict[str, str] | None, **kwargs: Any) -> KeyboardButton:
    extra: dict[str, Any] = {}
    if style:
        if style.get("style") in STYLES:
            extra["style"] = style["style"]
        if style.get("emoji_id"):
            extra["icon_custom_emoji_id"] = style["emoji_id"]
    return KeyboardButton(text=text, **extra, **kwargs)


def available(cfg: dict[str, Any]) -> set[str]:
    hidden = set()
    if not cfg.get("trial_enabled"):
        hidden.add("trial")
    if not cfg.get("referral_enabled"):
        hidden.add("referral")
    if not (cfg.get("download_apps") or []):
        hidden.add("apps")
    if not get_settings().public_url.startswith("https://"):
        hidden.add("miniapp")
    return set(BUTTONS) - hidden


def main_menu(t: Texts, cfg: dict[str, Any], *, is_staff: bool) -> ReplyKeyboardMarkup:
    layout = cfg.get("menu_layout") or DEFAULT_LAYOUT
    styles: dict[str, dict[str, str]] = cfg.get("button_styles") or {}
    shown = available(cfg)
    rows: list[list[KeyboardButton]] = []
    for row in layout:
        buttons = []
        for item in row:
            if item not in shown:
                continue
            if item == "miniapp":
                url = f"{get_settings().public_url}/app/"
                buttons.append(styled(t(BUTTONS[item]), styles.get(item), web_app=WebAppInfo(url=url)))
            else:
                buttons.append(styled(t(BUTTONS[item]), styles.get(item)))
        if buttons:
            rows.append(buttons)
    if is_staff:
        rows.append([styled(t("btn_admin"), styles.get("admin"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def validate_help(value: Any) -> list[dict[str, str]]:
    """Help topics behind the guide button: a title and a text, or a title and a link (BOT-05)."""
    if not isinstance(value, list) or len(value) > 20:
        raise LayoutError("help_items_invalid")
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise LayoutError("help_items_invalid")
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or len(title) > 48 or len(text) > 3000 or (not text and not url):
            raise LayoutError("help_items_invalid")
        if url and not re.match(r"^https://\S{4,300}$", url):
            raise LayoutError("help_items_invalid")
        clean.append({"title": title, "text": text, "url": url})
    return clean
