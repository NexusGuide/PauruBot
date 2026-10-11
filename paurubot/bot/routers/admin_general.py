"""General settings hub and the real main options (ADM-GS).

Only settings that PauruBot actually enforces appear here. Every switch carries the desired
value, so a repeated or stale tap cannot flip a setting back. Turning the bot off and asking
every customer to accept the rules again need an explicit confirmation.
"""

from __future__ import annotations

import re
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.utils import StaffOnly, show
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import LOCALES, Texts

router = Router(name="admin_general")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

SECTION = "gset"
PERMISSION = "settings.write"
HUB, MAIN = 0, 1  # where to return after a change
MENU_MODES = ("reply", "inline", "miniapp")
IRAN = ["98"]
SUPPORT_SHAPE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
CRONS_PAGE = 1  # index of "crons" in admin_config.CONFIG, checked by tests


class GeneralStates(StatesGroup):
    support = State()


def _cb(action: str, id: int = 0, page: int = 0) -> str:
    return Admin(section=SECTION, action=action, id=id, page=page).pack()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data)


def on_off(t: Texts, value: bool) -> str:
    return t("adm_gs_on") if value else t("adm_gs_off")


def phone_scope(cfg: dict[str, Any]) -> str:
    codes = [str(code).lstrip("+") for code in cfg.get("phone_country_codes") or []]
    if not codes:
        return "any"
    return "iran" if codes == IRAN else "custom"


def modes(cfg: dict[str, Any]) -> tuple[str, ...]:
    from paurubot.bot.menu import available

    return tuple(m for m in MENU_MODES if m != "miniapp" or "miniapp" in available(cfg))


def current_mode(cfg: dict[str, Any]) -> str:
    mode = str(cfg.get("menu_mode") or "reply")
    return mode if mode in modes(cfg) else "reply"


def next_mode(cfg: dict[str, Any]) -> int:
    options = modes(cfg)
    return MENU_MODES.index(options[(options.index(current_mode(cfg)) + 1) % len(options)])


def languages(t: Texts, cfg: dict[str, Any]) -> str:
    chosen = [code for code in LOCALES if code in (cfg.get("languages") or [])]
    return t("adm_gs_lang_sep").join(t(f"adm_gs_lang_{code}") for code in chosen) or "—"


def hub_kb(t: Texts, staff: StaffIdentity, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if staff.can(PERMISSION):
        enabled = bool(cfg.get("bot_enabled", True))
        support = str(cfg.get("support_username") or "") or t("adm_gs_unset")
        terms = bool(cfg.get("terms_required"))
        rows += [
            [_btn(t("adm_gs_bot", status=on_off(t, enabled)), _cb("bot", int(not enabled), HUB))],
            [
                _btn(t("adm_gs_main"), Admin(section="feat", action="cat", page=0).pack()),
                _btn(t("adm_hub_features"), Admin(section="feat", action="").pack()),
            ],
            [
                _btn(t("adm_gs_support", value=support), _cb("support")),
                _btn(t("adm_gs_langs", value=languages(t, cfg)), _cb("langs")),
            ],
            [
                _btn(
                    t("adm_gs_menu", value=t(f"adm_gs_mode_{current_mode(cfg)}")),
                    _cb("menu", next_mode(cfg), HUB),
                ),
                _btn(t("adm_gs_look"), Admin(section="look", action="").pack()),
            ],
            [
                _btn(t("adm_gs_terms", status=on_off(t, terms)), _cb("terms", int(not terms), HUB)),
                _btn(t("adm_gs_reterms"), _cb("reterms")),
            ],
            [
                _btn(t("adm_cfg_group_crons"), Admin(section="cfg", action="grp", page=CRONS_PAGE).pack()),
                _btn(t("adm_gs_values"), Admin(section="settings", action="show").pack()),
            ],
        ]
    ops = []
    if staff.can(PERMISSION):
        ops.append(_btn(t("adm_status"), Admin(section="status", action="").pack()))
    if staff.can("backups.manage"):
        ops.append(_btn(t("adm_backups"), Admin(section="backups", action="").pack()))
    if ops:
        rows.append(ops)
    url = get_settings().public_url.rstrip("/")
    if url and staff.can(PERMISSION):
        rows.append([InlineKeyboardButton(text=t("adm_gs_web"), url=f"{url}/admin/settings")])
    rows.append(
        [
            _btn(t("adm_ud_admin"), Admin(section="home").pack()),
            _btn(t("adm_ud_close"), _cb("close")),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_extra_rows(t: Texts, cfg: dict[str, Any]) -> list[list[InlineKeyboardButton]]:
    """Rows of the main-options table that are not plain feature switches."""
    scope = phone_scope(cfg)
    media = bool((cfg.get("start_media") or {}).get("file_id"))
    status = t(f"adm_gs_scope_{scope}")
    return [
        [
            _btn(status, _cb("iran", int(scope != "iran"), MAIN)),
            _btn(t("adm_gs_iran"), _cb("iran", int(scope != "iran"), MAIN)),
        ],
        [
            _btn(
                t("adm_gs_menu", value=t(f"adm_gs_mode_{current_mode(cfg)}")),
                _cb("menu", next_mode(cfg), MAIN),
            )
        ],
        [
            _btn(
                t("adm_gs_sticker", value=t("adm_gs_media_set" if media else "adm_gs_media_none")),
                Admin(section="look", action="start_set").pack(),
            )
        ],
    ]


def langs_kb(t: Texts, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    chosen = cfg.get("languages") or []
    rows = [[_btn(t("adm_feat_col_status"), _cb("noop")), _btn(t("adm_feat_col_title"), _cb("noop"))]]
    for index, code in enumerate(LOCALES):
        active = code in chosen
        data = _cb("lang", index, int(not active))
        rows.append([_btn(on_off(t, active), data), _btn(t(f"adm_gs_lang_{code}"), data)])
    rows.append([_btn(t("btn_back"), _cb("show"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(t: Texts, yes: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(t("adm_gs_confirm"), yes), _btn(t("btn_cancel"), _cb("show"))]]
    )


async def render(
    event: CallbackQuery | Message, t: Texts, staff: StaffIdentity, cfg: dict[str, Any], origin: int
) -> None:
    """origin 0 is this hub; origin n opens feature group n - 1 (1 is the main options)."""
    from paurubot.bot.routers.admin_hub import FEATURES, group_kb, group_text

    groups = list(FEATURES)
    if 1 <= origin <= len(groups):
        group = groups[origin - 1]
        await show(event, group_text(t, group), group_kb(t, group, cfg, staff))
        return
    await show(event, t("adm_gs_home"), hub_kb(t, staff, cfg))


def themes() -> tuple[str, ...]:
    from paurubot.bot.infocard import THEMES

    return tuple(THEMES)


def current_theme(cfg: dict[str, Any]) -> str:
    name = str(cfg.get("info_card_theme") or "")
    return name if name in themes() else themes()[0]


def theme_button(t: Texts, cfg: dict[str, Any], origin: int) -> InlineKeyboardButton:
    """Cycles the service info-card colour theme; the button carries the next theme."""
    names = themes()
    wanted = (names.index(current_theme(cfg)) + 1) % len(names)
    label = t("adm_gs_theme", value=t(f"adm_gs_theme_{current_theme(cfg)}"))
    return _btn(label, _cb("theme", wanted, origin))


async def _save(session: AsyncSession, shop: ShopRow, staff: StaffIdentity, key: str, value: Any) -> None:
    from paurubot.bot.routers.admin_tools import _save_setting

    await _save_setting(session, shop, staff, key, value)  # audited and committed


@router.callback_query(Admin.filter(F.section == SECTION), StaffOnly(PERMISSION))
async def general(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    from paurubot.bot.routers.admin_hub import FEATURES

    action, value, origin = callback_data.action, callback_data.id, callback_data.page
    if action != "lang" and not 0 <= origin <= len(FEATURES):
        origin = HUB
    if action == "noop":
        await call.answer()
        return
    if action == "close":
        await state.clear()
        await call.answer()
        await show(call, t("adm_gs_closed"))
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if action in {"", "show"}:
        await state.clear()
        await call.answer()
        await render(call, t, staff, cfg, HUB)
        return
    if action in {"bot", "botoff", "terms", "iran"} and value not in {0, 1}:
        await call.answer(t("button_expired"), show_alert=True)
        return
    if action == "bot":
        if value == 0 and cfg.get("bot_enabled", True):
            await call.answer()
            await show(call, t("adm_gs_bot_off_ask"), confirm_kb(t, _cb("botoff", 0, origin)))
            return
        if bool(cfg.get("bot_enabled", True)) != bool(value):
            await _save(session, shop, staff, "bot_enabled", bool(value))
    elif action == "botoff":
        if cfg.get("bot_enabled", True):
            await _save(session, shop, staff, "bot_enabled", False)
    elif action == "terms":
        if bool(cfg.get("terms_required")) != bool(value):
            await _save(session, shop, staff, "terms_required", bool(value))
    elif action == "iran":
        scope = phone_scope(cfg)
        if scope == "custom":
            await call.answer(t("adm_gs_scope_custom_alert"), show_alert=True)
            return
        wanted = list(IRAN) if value else []
        if (scope == "iran") != bool(value):
            await _save(session, shop, staff, "phone_country_codes", wanted)
    elif action == "menu":
        if not 0 <= value < len(MENU_MODES) or MENU_MODES[value] not in modes(cfg):
            await call.answer(t("button_expired"), show_alert=True)
            return
        if current_mode(cfg) != MENU_MODES[value]:
            await _save(session, shop, staff, "menu_mode", MENU_MODES[value])
    elif action == "theme":
        if not 0 <= value < len(themes()):
            await call.answer(t("button_expired"), show_alert=True)
            return
        if current_theme(cfg) != themes()[value]:
            await _save(session, shop, staff, "info_card_theme", themes()[value])
    elif action == "langs":
        await call.answer()
        await show(call, t("adm_gs_langs_title"), langs_kb(t, cfg))
        return
    elif action == "lang":
        if not 0 <= value < len(LOCALES):
            await call.answer(t("button_expired"), show_alert=True)
            return
        code = LOCALES[value]
        chosen = [c for c in LOCALES if c in (cfg.get("languages") or [])]
        wanted = [c for c in LOCALES if (c in chosen and c != code) or (c == code and origin == 1)]
        if code == shop.default_locale and code not in wanted:
            await call.answer(t("adm_gs_lang_default"), show_alert=True)
            return
        if not wanted:
            await call.answer(t("adm_gs_lang_last"), show_alert=True)
            return
        if wanted != chosen:
            await _save(session, shop, staff, "languages", wanted)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await call.answer(t("adm_saved"))
        await show(call, t("adm_gs_langs_title"), langs_kb(t, cfg))
        return
    elif action == "reterms":
        await call.answer()
        version = int(cfg.get("terms_version") or 1)
        await show(
            call,
            t("adm_gs_reterms_ask", version=version, next=version + 1),
            confirm_kb(t, _cb("retermsok", version)),
        )
        return
    elif action == "retermsok":
        version = int(cfg.get("terms_version") or 1)
        if value != version:
            await call.answer(t("button_expired"), show_alert=True)
            await render(call, t, staff, cfg, HUB)
            return
        await _save(session, shop, staff, "terms_version", version + 1)
    elif action == "support":
        await call.answer()
        await state.set_state(GeneralStates.support)
        await state.update_data(gs_shop=shop.id)
        current = esc(str(cfg.get("support_username") or "") or t("adm_gs_unset"))
        await show(
            call,
            t("adm_gs_support_ask", value=current),
            InlineKeyboardMarkup(inline_keyboard=[[_btn(t("btn_cancel"), _cb("show"))]]),
        )
        return
    else:
        await call.answer(t("button_expired"), show_alert=True)
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    await call.answer(t("adm_saved"))
    await render(call, t, staff, cfg, origin)


@router.message(GeneralStates.support, F.text, StaffOnly(PERMISSION))
async def support_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if data.get("gs_shop") != shop.id:
        await state.clear()
        await message.answer(t("button_expired"))
        return
    text = (message.text or "").strip()
    if text in {"-", "0"}:
        value = ""
    elif SUPPORT_SHAPE.match(text):
        value = "@" + text.lstrip("@")
    else:
        await message.answer(t("adm_gs_support_invalid"))
        return
    await state.clear()
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if str(cfg.get("support_username") or "") != value:
        await _save(session, shop, staff, "support_username", value)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
    from paurubot.bot.routers.admin_navigation import menu

    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await render(message, t, staff, cfg, HUB)
