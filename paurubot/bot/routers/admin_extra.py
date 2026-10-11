"""Wallet transfer, referral, custom purchase and service options from the bot (ADM-15)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin_config import _ask, _back, _btn, save, shown
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, parse_int
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="admin_extra")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

SECTION = "xcfg"
PERMISSION = "settings.write"
GROUPS: dict[str, tuple[str, ...]] = {
    "transfer": ("wallet_transfer_enabled", "wallet_transfer_min", "wallet_transfer_daily_max"),
    "referral": (
        "referral_enabled",
        "referral_percent",
        "referral_repeat_percent",
        "referral_every_purchase",
        "referral_min_order",
        "referral_daily_max",
        "referral_monthly_max",
        "start_gift_amount",
        "start_gift_referred_only",
    ),
    "custom": (
        "custom_buy_enabled",
        "custom_base_price",
        "custom_gb_price",
        "custom_day_price",
        "custom_min_gb",
        "custom_max_gb",
        "custom_min_days",
        "custom_max_days",
    ),
    "services": (
        "miniapp_enabled",
        "extras_enabled",
        "extra_gb_price",
        "extra_day_price",
        "upgrade_enabled",
        "move_enabled",
        "move_price",
        "self_delete_enabled",
        "self_delete_refund_percent",
    ),
}
KEYS: tuple[str, ...] = tuple(key for keys in GROUPS.values() for key in keys)
DEFAULT_BOUNDS = (0, 10**10)
BOUNDS: dict[str, tuple[int, int]] = {
    "referral_percent": (0, 100),
    "referral_repeat_percent": (0, 100),
    "self_delete_refund_percent": (0, 100),
    "custom_min_gb": (1, 10_000),
    "custom_max_gb": (1, 10_000),
    "custom_min_days": (1, 3_650),
    "custom_max_days": (1, 3_650),
}
LIMITS = (("custom_min_gb", "custom_max_gb"), ("custom_min_days", "custom_max_days"))
NEEDS = {"custom_buy_enabled": "custom_panel_id"}


def extra_buttons(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    return [(t("adm_x_btn"), Admin(section=SECTION))] if staff.can(PERMISSION) else []


def group_of(key: str) -> str:
    return next(name for name, keys in GROUPS.items() if key in keys)


def parse_extra(key: str, raw: str) -> tuple[Any, bool]:
    """(value, ok) for a typed number; "-" restores the default."""
    raw = raw.strip()
    if raw == "-":
        return shop_settings.SPECS[key].default, True
    number = parse_int(raw)
    low, high = BOUNDS.get(key, DEFAULT_BOUNDS)
    if number is None or not low <= number <= high:
        return None, False
    return number, True


def pair_ok(key: str, value: int, cfg: dict[str, Any]) -> bool:
    """A minimum may not pass its maximum, and the other way around."""
    for low_key, high_key in LIMITS:
        if key == low_key:
            return value <= int(cfg.get(high_key) or 0)
        if key == high_key:
            return int(cfg.get(low_key) or 0) <= value
    return True


def can_turn_on(key: str, cfg: dict[str, Any]) -> bool:
    need = NEEDS.get(key)
    return need is None or cfg.get(need) is not None


def home_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, name in enumerate(GROUPS):
        kb.row(_btn(t(f"adm_x_group_{name}"), Admin(section=SECTION, action="grp", page=index)))
    kb.row(_back(t, Admin(section="home")))
    return kb.as_markup()


def group_kb(t: Texts, group: str, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in GROUPS[group]:
        name = t(f"adm_x_{key}")
        label = f"{name}: {shown(key, cfg.get(key))}"
        kb.row(_btn(label, Admin(section=SECTION, action="set", id=KEYS.index(key))))
    kb.row(_back(t, Admin(section=SECTION)))
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == SECTION), StaffOnly(PERMISSION))
async def extra_config(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    groups = list(GROUPS)
    if action == "set":
        if not 0 <= callback_data.id < len(KEYS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        key = KEYS[callback_data.id]
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        if shop_settings.SPECS[key].kind is not bool:
            await call.answer()
            await state.set_state(AdminStates.extra_value)
            await state.update_data(extra_key=key)
            name = esc(t(f"adm_x_{key}"))
            await _ask(call, t("adm_x_ask", name=name, value=esc(shown(key, cfg.get(key)))), t)
            return
        value = not bool(cfg.get(key))
        if value and not can_turn_on(key, cfg):
            await call.answer(t("adm_x_needs_setup"), show_alert=True)
            return
        await save(session, shop, staff, key, value)
        await call.answer(t("adm_saved"))
        group = group_of(key)
        fresh = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, t(f"adm_x_group_{group}"), group_kb(t, group, fresh))
        return
    await call.answer()
    await state.clear()
    if action == "grp" and 0 <= callback_data.page < len(groups):
        group = groups[callback_data.page]
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, t(f"adm_x_group_{group}"), group_kb(t, group, cfg))
        return
    await show(call, t("adm_x_title"), home_kb(t))


@router.message(AdminStates.extra_value, F.text)
async def extra_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    key = str(data.get("extra_key") or "")
    if key not in KEYS or not staff.can(PERMISSION) or shop_settings.SPECS[key].kind is bool:
        await state.clear()
        return
    value, ok = parse_extra(key, message.text or "")
    if not ok:
        low, high = BOUNDS.get(key, DEFAULT_BOUNDS)
        await message.answer(t("adm_x_range", low=low, high=high))
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if value is not None and not pair_ok(key, value, cfg):
        await message.answer(t("adm_x_pair"))
        return
    await state.clear()
    await save(session, shop, staff, key, value)
    group = group_of(key)
    fresh = await shop_settings.load(session, shop.id, fresh=True)
    await message.answer(t("adm_saved"), reply_markup=menu(t, fresh, staff))
    await message.answer(t(f"adm_x_group_{group}"), reply_markup=group_kb(t, group, fresh))
