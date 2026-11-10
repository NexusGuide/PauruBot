"""The main reply keyboard: shop-defined layout (BOT-02) and button colours / custom emoji (BOT-03)."""

from __future__ import annotations

import re
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

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
    "accounts": "btn_accounts",
    "tickets": "btn_tickets",
    "guide": "btn_guide",
    "apps": "btn_apps",
    "language": "btn_language",
    "wheel": "btn_wheel",
    "lottery": "btn_lottery",
}

DEFAULT_LAYOUT: list[list[str]] = [
    ["miniapp"],
    ["buy", "services"],
    ["wallet", "trial"],
    ["referral", "reseller", "accounts"],
    ["tickets", "guide"],
    ["wheel", "lottery"],
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


def button_style(style: dict[str, str] | None) -> dict[str, Any]:
    """Telegram style fields shared by reply and inline buttons."""
    extra: dict[str, Any] = {}
    if style:
        if style.get("style") in STYLES:
            extra["style"] = style["style"]
        if style.get("emoji_id"):
            extra["icon_custom_emoji_id"] = style["emoji_id"]
    return extra


def styled(text: str, style: dict[str, str] | None, **kwargs: Any) -> KeyboardButton:
    return KeyboardButton(text=text, **button_style(style), **kwargs)


def available(cfg: dict[str, Any]) -> set[str]:
    hidden = set()
    if not cfg.get("trial_enabled"):
        hidden.add("trial")
    if not cfg.get("referral_enabled"):
        hidden.add("referral")
    if not (cfg.get("download_apps") or []):
        hidden.add("apps")
    if not cfg.get("wheel_enabled"):
        hidden.add("wheel")
    if not cfg.get("lottery_enabled"):
        hidden.add("lottery")
    if not cfg.get("panel_accounts_enabled"):
        hidden.add("accounts")
    if not get_settings().public_url.startswith("https://") or not cfg.get("miniapp_enabled", True):
        hidden.add("miniapp")
    if len(cfg.get("languages") or []) < 2:
        hidden.add("language")  # nothing to choose between
    if cfg.get("own_bot"):
        # A customer's dedicated bot only sells: it follows the main shop's language and has no
        # resellers or admin accounts of its own (OPB).
        hidden.update({"language", "reseller", "accounts"})
    return set(BUTTONS) - hidden


def menu_mode(cfg: dict[str, Any]) -> str:
    """reply, inline or miniapp (BOT-12); the Mini App mode needs an https address."""
    mode = str(cfg.get("menu_mode") or "reply")
    if mode == "miniapp" and "miniapp" not in available(cfg):
        return "inline"
    return mode if mode in {"reply", "inline", "miniapp"} else "reply"


def inline_menu(t: Texts, cfg: dict[str, Any], *, is_staff: bool = False) -> InlineKeyboardMarkup:
    """The main menu as glass buttons under a message; each one acts like its reply-keyboard twin."""
    from paurubot.bot.callbacks import Admin, Menu

    layout = layout_of(cfg)
    shown = available(cfg)
    styles = cfg.get("button_styles") or {}
    rows: list[list[InlineKeyboardButton]] = []
    for row in layout:
        buttons = []
        for item in row:
            if item not in shown:
                continue
            if item == "miniapp":
                url = f"{get_settings().public_url.rstrip('/')}/app/"
                buttons.append(
                    InlineKeyboardButton(
                        text=t(BUTTONS[item]), web_app=WebAppInfo(url=url), **button_style(styles.get(item))
                    )
                )
            else:
                buttons.append(
                    InlineKeyboardButton(
                        text=t(BUTTONS[item]),
                        callback_data=Menu(item=item).pack(),
                        **button_style(styles.get(item)),
                    )
                )
        if buttons:
            rows.append(buttons)
    if is_staff:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("btn_admin"),
                    callback_data=Admin(section="home").pack(),
                    **button_style(styles.get("admin")),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miniapp_button(
    t: Texts, cfg: dict[str, Any] | None = None, *, is_staff: bool = False
) -> InlineKeyboardMarkup:
    url = f"{get_settings().public_url.rstrip('/')}/app/"
    from paurubot.bot.callbacks import Admin

    styles = (cfg or {}).get("button_styles") or {}
    rows = [
        [
            InlineKeyboardButton(
                text=t("btn_miniapp_open"), web_app=WebAppInfo(url=url), **button_style(styles.get("miniapp"))
            )
        ]
    ]
    if is_staff:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("btn_admin"),
                    callback_data=Admin(section="home").pack(),
                    **button_style(styles.get("admin")),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def layout_of(cfg: dict[str, Any]) -> list[list[str]]:
    """The shop's own arrangement, else the default. A customer's dedicated bot keeps the Mini App
    button last, right above its manager's admin button (OPB)."""
    layout: list[list[str]] = cfg.get("menu_layout") or DEFAULT_LAYOUT
    if cfg.get("own_bot") and not cfg.get("menu_layout"):
        rest = [[item for item in row if item != "miniapp"] for row in layout]
        return [row for row in rest if row] + [["miniapp"]]
    return layout


def main_menu(t: Texts, cfg: dict[str, Any], *, is_staff: bool) -> ReplyKeyboardMarkup:
    layout = layout_of(cfg)
    styles: dict[str, dict[str, str]] = cfg.get("button_styles") or {}
    shown = available(cfg)
    rows: list[list[KeyboardButton]] = []
    for row in layout:
        buttons = []
        for item in row:
            if item not in shown:
                continue
            # The Mini App button is a plain one: its handler answers with an inline web-app button.
            # Telegram signs the user into a Mini App opened from an inline or menu button, never
            # into one opened from a reply-keyboard button, and the app can't work signed out.
            buttons.append(styled(t(BUTTONS[item]), styles.get(item)))
        if buttons:
            rows.append(buttons)
    if is_staff:
        rows.append([styled(t("btn_admin"), styles.get("admin"))])
    # Not persistent: the phone's back button and the keyboard icon must be able to fold it away,
    # or it covers half the chat on small screens and nothing can close it.
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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
