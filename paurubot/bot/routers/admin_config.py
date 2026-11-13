"""Shop setup from the bot (ADM-13): payment gateway credentials, card accounts, scheduled jobs,
discount and gift codes and per-panel options, so daily work does not need the web panel."""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from contextlib import suppress
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_hub import can_enable
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import settings as shop_settings
from paurubot.core.cards import CARD_PICKS, digits, luhn_ok, masked
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import DiscountCode, DiscountKind, GiftCode, Panel, UsernameMethod
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="admin_config")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

PAGE = 10
MAX_CARDS = 10
CODE_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
PREFIX_RE = re.compile(r"[a-z0-9_]{0,16}")
PERCENT_SIGNS = ("%", "\u066a")
METHODS = list(UsernameMethod)
CHOICES: dict[str, tuple[str, ...]] = {"card_pick": CARD_PICKS}

_GROUPS: dict[str, tuple[str, ...]] = {
    "card": (
        "card_enabled",
        "card_pick",
        "card_random_tail",
        "card_auto_approve",
        "card_auto_max_amount",
        "card_auto_min_history",
        "card_auto_approve_after_minutes",
        "card_verify_enabled",
        "card_verify_min_amount",
        "receipt_duplicate_block",
    ),
    "crons": (
        "order_expire_minutes",
        "payment_expire_minutes",
        "usage_sync_minutes",
        "warn_days",
        "warn_remaining_percent",
        "delete_expired_after_days",
        "delete_warn_days",
        "daily_report_hour",
        "log_retention_days",
        "wheel_every_hours",
        "business_reply_every_hours",
        "trial_daily_quota",
        "trial_min_account_minutes",
    ),
    "stars": ("stars_enabled", "stars_rate"),
    "crypto": (
        "crypto_enabled",
        "tron_address",
        "ton_address",
        "trongrid_api_key",
        "toncenter_api_key",
        "usdt_rate",
        "trx_rate",
        "ton_rate",
        "fx_markup_percent",
    ),
    "zarinpal": ("zarinpal_enabled", "zarinpal_merchant", "zarinpal_sandbox"),
    "nowpayments": ("nowpayments_enabled", "nowpayments_api_key", "nowpayments_ipn_secret"),
    "plisio": ("plisio_enabled", "plisio_api_key"),
    "aqayepardakht": ("aqayepardakht_enabled", "aqayepardakht_pin"),
    "tetra98": ("tetra98_enabled", "tetra98_api_key"),
    "variza": ("variza_enabled", "variza_api_token", "variza_webhook_secret"),
    "abangateway": (
        "abangateway_enabled",
        "abangateway_url",
        "abangateway_api_key",
        "abangateway_rial",
    ),
    "blupal": ("blupal_enabled", "blupal_api_key"),
    "tronado": (
        "tronado_enabled",
        "tronado_api_key",
        "tronado_ipn_key",
        "tronado_wallet",
        "tronado_wage_percent",
    ),
    "cubepay": ("cubepay_enabled", "cubepay_api_key", "cubepay_fee_percent"),
    "atlaspay": ("atlaspay_enabled", "atlaspay_api_key"),
}
# Only keys this build knows about; a gateway missing from SPECS drops out of the menu.
CONFIG: dict[str, tuple[str, ...]] = {}
for _name, _keys in _GROUPS.items():
    _known = tuple(key for key in _keys if key in shop_settings.SPECS)
    if _known:
        CONFIG[_name] = _known
CONFIG_KEYS: list[str] = [key for keys in CONFIG.values() for key in keys]

BOUNDS: dict[str, tuple[int, int]] = {
    "tronado_wage_percent": (0, 100),
    "cubepay_fee_percent": (0, 100),
    "fx_markup_percent": (0, 100),
    "warn_remaining_percent": (0, 100),
    "daily_report_hour": (0, 23),
    "order_expire_minutes": (5, 7 * 24 * 60),
    "payment_expire_minutes": (5, 7 * 24 * 60),
    "usage_sync_minutes": (1, 24 * 60),
    "card_auto_approve_after_minutes": (0, 7 * 24 * 60),
    "card_auto_min_history": (0, 1_000),
    "warn_days": (0, 30),
    "delete_expired_after_days": (0, 365),
    "delete_warn_days": (0, 30),
    "log_retention_days": (1, 3650),
    "wheel_every_hours": (1, 24 * 30),
    "business_reply_every_hours": (1, 24 * 30),
    "trial_daily_quota": (0, 100_000),
    "trial_min_account_minutes": (0, 7 * 24 * 60),
}
DEFAULT_BOUNDS = (0, 10**12)


def config_buttons(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    out: list[tuple[str, Admin]] = []
    if staff.can("settings.write"):
        out.append((t("adm_cfg_btn"), Admin(section="cfg")))
        out.append((t("adm_cards_btn"), Admin(section="cards")))
    if staff.can("catalog.write"):
        out.append((t("adm_dc_btn"), Admin(section="dc")))
    if staff.can("wallet.adjust"):
        out.append((t("adm_gc_btn"), Admin(section="gc")))
    if staff.can("panels.write"):
        out.append((t("adm_pcfg_btn"), Admin(section="pcfg")))
    # Local import: admin_growth reuses this module's helpers.
    from paurubot.bot.routers.admin_growth import growth_buttons

    out += growth_buttons(t, staff)
    return out


def _btn(text: str, target: Admin) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=target.pack())


def _back(t: Texts, target: Admin) -> InlineKeyboardButton:
    return _btn(t("btn_back"), target)


async def _ask(call: CallbackQuery, text: str, t: Texts) -> None:
    if isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=keyboards.cancel(t))


# --- settings: gateways, card, crons ---------------------------------------------------------


def shown(key: str, value: Any) -> str:
    """A setting as staff see it; secrets only say whether they are set."""
    if shop_settings.SPECS[key].secret:
        return "\u2705" if value else "\u2014"
    if isinstance(value, bool):
        return "\u2705" if value else "\u274c"
    if value is None or value == "":
        return "\u2014"
    return str(value)


def parse_value(key: str, raw: str) -> tuple[Any, str | None]:
    """The value staff typed for a setting, or the text key explaining why it was refused."""
    spec = shop_settings.SPECS[key]
    if raw == "-":
        cleared = "" if spec.kind is str else spec.default
        return cleared, None
    if spec.kind is bool:
        try:
            return shop_settings.coerce(key, raw), None
        except shop_settings.SettingError:
            return None, "adm_cfg_invalid"
    if spec.kind is int:
        number = parse_int(raw)
        low, high = BOUNDS.get(key, DEFAULT_BOUNDS)
        if number is None or not low <= number <= high:
            return None, "adm_cfg_range"
        return number, None
    if spec.kind is not str or not raw or len(raw) > 255 or "\n" in raw:
        return None, "adm_cfg_invalid"
    if key.endswith("_url") and not raw.startswith("https://"):
        return None, "adm_cfg_invalid"
    if key in CHOICES and raw not in CHOICES[key]:
        return None, "adm_cfg_invalid"
    return raw, None


def _audit(key: str, value: Any) -> Any:
    if shop_settings.SPECS[key].secret:
        return "***" if value else ""
    return value


async def save(session: AsyncSession, shop: ShopRow, staff: StaffIdentity, key: str, value: Any) -> None:
    """Store one setting and audit it; secret values never reach the audit log."""
    before = await shop_settings.get(session, shop.id, key)
    await shop_settings.set_many(session, shop.id, {key: value})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.update",
        entity="shop",
        entity_id=shop.id,
        before={key: _audit(key, before)},
        after={key: _audit(key, value)},
    )
    await session.commit()


def _group_of(key: str) -> str:
    return next(name for name, keys in CONFIG.items() if key in keys)


def group_kb(t: Texts, group: str, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in CONFIG[group]:
        if shop_settings.SPECS[key].kind is bool:
            action = "tog"
        else:
            action = "pick" if key in CHOICES else "ask"
        name = t(f"adm_cfg_{key}")
        label = f"{name}: {shown(key, cfg.get(key))}"
        kb.row(_btn(label, Admin(section="cfg", action=action, id=CONFIG_KEYS.index(key))))
    kb.row(_back(t, Admin(section="cfg")))
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == "cfg"), StaffOnly("settings.write"))
async def config(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    groups = list(CONFIG)
    action = callback_data.action
    if action in {"tog", "pick", "ask"}:
        if not 0 <= callback_data.id < len(CONFIG_KEYS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        key = CONFIG_KEYS[callback_data.id]
        if action == "tog" and shop_settings.SPECS[key].kind is not bool:
            action = "ask"
        if action == "pick" and key not in CHOICES:
            action = "ask"
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        if action == "ask":
            await call.answer()
            await state.set_state(AdminStates.config_value)
            await state.update_data(config_key=key)
            name = esc(t(f"adm_cfg_{key}"))
            current = esc(shown(key, cfg.get(key)))
            ask = "adm_cfg_ask_secret" if shop_settings.SPECS[key].secret else "adm_cfg_ask"
            await _ask(call, t(ask, name=name, value=current), t)
            return
        value: Any
        if action == "tog":
            value = not bool(cfg.get(key))
            if value and not can_enable(key, cfg):
                await call.answer(t("adm_feat_needs_setup"), show_alert=True)
                return
        else:
            options = CHOICES[key]
            current_value = str(cfg.get(key) or "")
            step = options.index(current_value) + 1 if current_value in options else 0
            value = options[step % len(options)]
        await save(session, shop, staff, key, value)
        await call.answer(t("adm_saved"))
        group = _group_of(key)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, t(f"adm_cfg_group_{group}"), group_kb(t, group, cfg))
        return
    await call.answer()
    if action == "grp" and 0 <= callback_data.page < len(groups):
        group = groups[callback_data.page]
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, t(f"adm_cfg_group_{group}"), group_kb(t, group, cfg))
        return
    kb = InlineKeyboardBuilder()
    for index, name in enumerate(groups):
        data = Admin(section="cfg", action="grp", page=index)
        kb.button(text=t(f"adm_cfg_group_{name}"), callback_data=data)
    kb.adjust(2)
    kb.row(_back(t, Admin(section="home")))
    await show(call, t("adm_cfg_title"), kb.as_markup())


@router.message(AdminStates.config_value, F.text)
async def config_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    key = str(data.get("config_key") or "")
    if key not in CONFIG_KEYS or not staff.can("settings.write"):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if shop_settings.SPECS[key].secret:
        # The credential should not stay in the chat history.
        with suppress(Exception):
            await message.delete()
    value, problem = parse_value(key, raw)
    if problem:
        low, high = BOUNDS.get(key, DEFAULT_BOUNDS)
        await message.answer(t(problem, low=low, high=high))
        return
    await state.clear()
    await save(session, shop, staff, key, value)
    group = _group_of(key)
    fresh = await shop_settings.load(session, shop.id, fresh=True)
    await message.answer(t("adm_saved"), reply_markup=menu(t, fresh, staff))
    await message.answer(t(f"adm_cfg_group_{group}"), reply_markup=group_kb(t, group, fresh))


# --- card accounts ---------------------------------------------------------------------------


def card_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in cfg.get("card_accounts") or [] if isinstance(item, dict)]


def card_line(item: dict[str, Any]) -> str:
    number = digits(str(item.get("number") or ""))
    label = masked(number) if len(number) == 16 else number
    icon = "⚪️" if item.get("disabled") else "🟢"
    return f"{icon} {label} {item.get('bank') or ''} {item.get('holder') or ''}".strip()


def parse_card(text: str) -> dict[str, str] | None:
    """`NUMBER | HOLDER | BANK`; the number must pass the Luhn check."""
    parts = [part.strip() for part in text.split("|")]
    number = digits(parts[0])
    if len(number) != 16 or not luhn_ok(number):
        return None
    holder = parts[1][:64] if len(parts) > 1 else ""
    bank = parts[2][:32] if len(parts) > 2 else ""
    return {"number": number, "holder": holder, "bank": bank}


@router.callback_query(Admin.filter(F.section == "cards"), StaffOnly("settings.write"))
async def cards_view(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    accounts = card_accounts(cfg)
    action, index = callback_data.action, callback_data.id
    if action == "add":
        if len(accounts) >= MAX_CARDS:
            await call.answer(t("adm_cards_full", count=MAX_CARDS), show_alert=True)
            return
        await call.answer()
        await state.set_state(AdminStates.card_account)
        await _ask(call, t("adm_cards_ask"), t)
        return
    if action in {"tog", "del"}:
        if not 0 <= index < len(accounts):
            await call.answer(t("button_expired"), show_alert=True)
            return
        if action == "del" and not callback_data.page:
            await call.answer()
            yes = Admin(section="cards", action="del", id=index, page=1).pack()
            no = Admin(section="cards").pack()
            text = t("adm_cards_delete_confirm", card=esc(card_line(accounts[index])))
            await show(call, text, keyboards.confirm(t, yes, no))
            return
        if action == "tog":
            accounts[index]["disabled"] = not accounts[index].get("disabled")
        else:
            accounts.pop(index)
        await save(session, shop, staff, "card_accounts", accounts)
        await call.answer(t("adm_saved"))
    else:
        await call.answer()
    kb = InlineKeyboardBuilder()
    lines = [t("adm_cards_title")]
    for i, item in enumerate(accounts):
        lines.append(f"{i + 1}. {esc(card_line(item))}")
        toggle = t("adm_cards_on") if item.get("disabled") else t("adm_cards_off")
        kb.row(
            _btn(f"{toggle} #{i + 1}", Admin(section="cards", action="tog", id=i)),
            _btn(f"{t('adm_cards_delete')} #{i + 1}", Admin(section="cards", action="del", id=i)),
        )
    if not accounts:
        lines.append(t("adm_cards_empty"))
    kb.row(_btn(t("adm_cards_add"), Admin(section="cards", action="add")))
    kb.row(_back(t, Admin(section="home")))
    await show(call, "\n".join(lines), kb.as_markup())


@router.message(AdminStates.card_account, F.text)
async def card_add(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not staff.can("settings.write"):
        await state.clear()
        return
    card = parse_card(message.text or "")
    if card is None:
        await message.answer(t("adm_cards_invalid"))
        return
    accounts = card_accounts(await shop_settings.load(session, shop.id, fresh=True))
    if any(digits(str(item.get("number") or "")) == card["number"] for item in accounts):
        await message.answer(t("adm_cards_exists"))
        return
    await state.clear()
    if len(accounts) >= MAX_CARDS:
        await message.answer(t("adm_cards_full", count=MAX_CARDS), reply_markup=menu(t, cfg, staff))
        return
    accounts.append(card)
    await save(session, shop, staff, "card_accounts", accounts)
    back = keyboards.back(t, Admin(section="cards").pack())
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(t("adm_cards_added", card=masked(card["number"])), reply_markup=back)


# --- discount and gift codes -----------------------------------------------------------------


def parse_code(text: str, *, percent_ok: bool) -> dict[str, Any] | None:
    """`CODE VALUE [USES] [DAYS]`. `*` as the code picks a random one, a `%` after the value makes
    it a percentage (discount codes only) and zero uses or days mean no limit."""
    parts = text.split()
    if not 2 <= len(parts) <= 4:
        return None
    code = secrets.token_hex(4).upper() if parts[0] == "*" else parts[0]
    if not CODE_RE.fullmatch(code):
        return None
    raw = parts[1]
    percent = raw.endswith(PERCENT_SIGNS)
    if percent and not percent_ok:
        return None
    value = parse_int(raw.rstrip("".join(PERCENT_SIGNS)))
    numbers = [parse_int(part) for part in parts[2:]]
    if value is None or any(number is None or number < 0 for number in numbers):
        return None
    if percent and not 1 <= value <= 100:
        return None
    if not percent and not 1_000 <= value <= 100_000_000:
        return None
    uses = numbers[0] if numbers else 0
    days = numbers[1] if len(numbers) > 1 else 0
    return {"code": code, "value": value, "percent": percent, "uses": uses or None, "days": days or None}


def code_line(row: DiscountCode | GiftCode) -> str:
    if isinstance(row, DiscountCode):
        worth = f"{row.value}%" if row.kind == DiscountKind.PERCENT else money(row.value)
    else:
        worth = money(row.amount)
    icon = "🟢" if row.is_active else "⚪️"
    uses = f"{row.used_count}/{row.max_uses}" if row.max_uses else str(row.used_count)
    return f"{icon} {row.code} \u00b7 {worth} \u00b7 {uses}"


def code_text(t: Texts, row: DiscountCode | GiftCode) -> str:
    expires = jdate(row.expires_at) if row.expires_at else t("unlimited")
    return t("adm_code_card", line=esc(code_line(row)), expires=expires)


def code_kb(t: Texts, section: str, row: DiscountCode | GiftCode) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = t("adm_code_off") if row.is_active else t("adm_code_on")
    kb.row(_btn(toggle, Admin(section=section, action="tog", id=row.id)))
    if not row.used_count:
        kb.row(_btn(t("adm_code_delete"), Admin(section=section, action="del", id=row.id)))
    kb.row(_back(t, Admin(section=section)))
    return kb.as_markup()


async def _load_code(
    session: AsyncSession, shop_id: int, section: str, code_id: int
) -> DiscountCode | GiftCode | None:
    row: DiscountCode | GiftCode | None
    if section == "dc":
        row = await session.get(DiscountCode, code_id)
    else:
        row = await session.get(GiftCode, code_id)
    if row is None or row.shop_id != shop_id:
        return None
    return row


async def _code_rows(
    session: AsyncSession, shop_id: int, section: str, page: int
) -> Sequence[DiscountCode | GiftCode]:
    if section == "dc":
        found = await session.scalars(
            select(DiscountCode)
            .where(DiscountCode.shop_id == shop_id, DiscountCode.customer_id.is_(None))
            .order_by(DiscountCode.id.desc())
            .offset(page * PAGE)
            .limit(PAGE + 1)
        )
        return list(found)
    gifts = await session.scalars(
        select(GiftCode)
        .where(GiftCode.shop_id == shop_id)
        .order_by(GiftCode.id.desc())
        .offset(page * PAGE)
        .limit(PAGE + 1)
    )
    return list(gifts)


async def _code_taken(session: AsyncSession, shop_id: int, section: str, code: str) -> bool:
    if section == "dc":
        query = select(DiscountCode.id).where(DiscountCode.shop_id == shop_id, DiscountCode.code == code)
    else:
        query = select(GiftCode.id).where(GiftCode.shop_id == shop_id, GiftCode.code == code)
    return await session.scalar(query) is not None


async def _change_code(
    session: AsyncSession, shop: ShopRow, staff: StaffIdentity, row: DiscountCode | GiftCode, action: str
) -> None:
    entity = "discount_code" if isinstance(row, DiscountCode) else "gift_code"
    row_id = row.id
    after: dict[str, Any] = {"code": row.code}
    if action == "tog":
        row.is_active = not row.is_active
        after["is_active"] = row.is_active
    else:
        after["deleted"] = True
        await session.delete(row)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=f"{entity}.{action}",
        entity=entity,
        entity_id=row_id,
        after=after,
    )
    await session.commit()


async def _codes(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    section, action = callback_data.section, callback_data.action
    page = max(0, callback_data.page) if action == "list" else 0
    if action == "add":
        await call.answer()
        await state.set_state(AdminStates.code_new)
        await state.update_data(code_section=section)
        await _ask(call, t(f"adm_{section}_ask"), t)
        return
    if action in {"view", "tog", "del"}:
        row = await _load_code(session, shop.id, section, callback_data.id)
        if row is None:
            await call.answer(t("button_expired"), show_alert=True)
            return
        if action == "del" and row.used_count:
            await call.answer(t("adm_code_used"), show_alert=True)
            return
        if action == "del" and not callback_data.page:
            await call.answer()
            yes = Admin(section=section, action="del", id=row.id, page=1).pack()
            no = Admin(section=section, action="view", id=row.id).pack()
            text = t("adm_code_delete_confirm", code=esc(row.code))
            await show(call, text, keyboards.confirm(t, yes, no))
            return
        if action == "view":
            await call.answer()
            await show(call, code_text(t, row), code_kb(t, section, row))
            return
        await _change_code(session, shop, staff, row, action)
        await call.answer(t("adm_saved"))
        if action == "tog":
            await show(call, code_text(t, row), code_kb(t, section, row))
            return
    else:
        await call.answer()
    rows = await _code_rows(session, shop.id, section, page)
    kb = InlineKeyboardBuilder()
    for item in rows[:PAGE]:
        kb.row(_btn(code_line(item), Admin(section=section, action="view", id=item.id)))
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(_btn("\u2039", Admin(section=section, action="list", page=page - 1)))
    if len(rows) > PAGE:
        nav.append(_btn("\u203a", Admin(section=section, action="list", page=page + 1)))
    if nav:
        kb.row(*nav)
    kb.row(_btn(t("adm_code_add"), Admin(section=section, action="add")))
    kb.row(_back(t, Admin(section="home")))
    text = t(f"adm_{section}_title")
    if not rows:
        text += "\n" + t("adm_code_empty")
    await show(call, text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "dc"), StaffOnly("catalog.write"))
async def discount_codes(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    await _codes(call, callback_data, session, shop, t, staff, state)


@router.callback_query(Admin.filter(F.section == "gc"), StaffOnly("wallet.adjust"))
async def gift_codes(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    await _codes(call, callback_data, session, shop, t, staff, state)


@router.message(AdminStates.code_new, F.text)
async def code_new(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    section = "gc" if data.get("code_section") == "gc" else "dc"
    if not staff.can("wallet.adjust" if section == "gc" else "catalog.write"):
        await state.clear()
        return
    parsed = parse_code(message.text or "", percent_ok=section == "dc")
    if parsed is None:
        await message.answer(t(f"adm_{section}_invalid"))
        return
    if await _code_taken(session, shop.id, section, parsed["code"]):
        await message.answer(t("adm_code_taken"))
        return
    expires = utcnow() + timedelta(days=parsed["days"]) if parsed["days"] else None
    row: DiscountCode | GiftCode
    if section == "dc":
        row = DiscountCode(
            shop_id=shop.id,
            code=parsed["code"],
            kind=DiscountKind.PERCENT if parsed["percent"] else DiscountKind.FIXED,
            value=parsed["value"],
            min_amount=0,
            max_uses=parsed["uses"],
            used_count=0,
            per_customer_limit=1,
            plan_ids=[],
            first_purchase_only=False,
            sticky=False,
            expires_at=expires,
            is_active=True,
            note="bot",
        )
    else:
        row = GiftCode(
            shop_id=shop.id,
            code=parsed["code"],
            amount=parsed["value"],
            max_uses=parsed["uses"],
            used_count=0,
            new_customers_only=False,
            expires_at=expires,
            is_active=True,
            note="bot",
        )
    session.add(row)
    await session.flush()
    entity = "discount_code" if section == "dc" else "gift_code"
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=f"{entity}.create",
        entity=entity,
        entity_id=row.id,
        after={"code": row.code, "value": parsed["value"], "percent": parsed["percent"]},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(code_text(t, row), reply_markup=code_kb(t, section, row))


# --- per-panel options -----------------------------------------------------------------------


def panel_text(t: Texts, panel: Panel) -> str:
    if panel.is_healthy is None:
        health = "🟡"
    else:
        health = "🟢" if panel.is_healthy else "🔴"
    return t(
        "adm_pcfg_card",
        name=esc(panel.name),
        kind=panel.kind.value,
        health=health,
        state=t("adm_on") if panel.is_active else t("adm_off"),
        capacity=panel.capacity or t("unlimited"),
        method=t(f"adm_pcfg_method_{panel.username_method.value}"),
        prefix=esc(panel.username_prefix or "\u2014"),
    )


def panel_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = t("adm_pcfg_off") if panel.is_active else t("adm_pcfg_on")
    kb.row(
        _btn(toggle, Admin(section="pcfg", action="tog", id=panel.id)),
        _btn(t("adm_pcfg_method"), Admin(section="pcfg", action="method", id=panel.id)),
    )
    kb.row(
        _btn(t("adm_pcfg_capacity"), Admin(section="pcfg", action="cap", id=panel.id)),
        _btn(t("adm_pcfg_prefix"), Admin(section="pcfg", action="prefix", id=panel.id)),
    )
    kb.row(_btn(t("adm_pm_manage"), Admin(section="pmgr", id=panel.id)))
    kb.row(_back(t, Admin(section="pcfg")))
    return kb.as_markup()


async def _panel_log(
    session: AsyncSession, shop: ShopRow, staff: StaffIdentity, panel: Panel, after: dict[str, Any]
) -> None:
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.update",
        entity="panel",
        entity_id=panel.id,
        after=after,
    )
    await session.commit()


@router.callback_query(Admin.filter(F.section == "pcfg"), StaffOnly("panels.write"))
async def panel_config(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action not in {"view", "tog", "method", "cap", "prefix"}:
        await call.answer()
        stmt = select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id)
        rows = list(await session.scalars(stmt))
        kb = InlineKeyboardBuilder()
        for item in rows:
            icon = "🟢" if item.is_active else "⚪️"
            kb.row(_btn(f"{icon} {item.name}", Admin(section="pcfg", action="view", id=item.id)))
        kb.row(_back(t, Admin(section="home")))
        text = t("adm_pcfg_title") if rows else t("adm_pcfg_empty")
        await show(call, text, kb.as_markup())
        return
    panel = await session.get(Panel, callback_data.id)
    if panel is None or panel.shop_id != shop.id:
        await call.answer(t("button_expired"), show_alert=True)
        return
    if action in {"cap", "prefix"}:
        await call.answer()
        await state.set_state(AdminStates.panel_value)
        await state.update_data(panel_id=panel.id, panel_field=action)
        await _ask(call, t(f"adm_pcfg_ask_{action}"), t)
        return
    if action == "view":
        await call.answer()
    else:
        after: dict[str, Any]
        if action == "tog":
            panel.is_active = not panel.is_active
            after = {"is_active": panel.is_active}
        else:
            index = METHODS.index(panel.username_method)
            panel.username_method = METHODS[(index + 1) % len(METHODS)]
            after = {"username_method": panel.username_method.value}
        await _panel_log(session, shop, staff, panel, after)
        await call.answer(t("adm_saved"))
    await show(call, panel_text(t, panel), panel_kb(t, panel))


@router.message(AdminStates.panel_value, F.text)
async def panel_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    field = str(data.get("panel_field") or "")
    panel = await session.get(Panel, int(data.get("panel_id") or 0))
    allowed = staff.can("panels.write") and field in {"cap", "prefix"}
    if panel is None or panel.shop_id != shop.id or not allowed:
        await state.clear()
        return
    raw = (message.text or "").strip()
    after: dict[str, Any]
    if field == "cap":
        number = 0 if raw == "-" else parse_int(raw)
        if number is None or not 0 <= number <= 1_000_000:
            await message.answer(t("adm_cfg_range", low=0, high=1_000_000))
            return
        panel.capacity = number or None
        after = {"capacity": panel.capacity}
    else:
        prefix = "" if raw == "-" else raw.lower()
        if not PREFIX_RE.fullmatch(prefix):
            await message.answer(t("adm_cfg_invalid"))
            return
        panel.username_prefix = prefix
        after = {"username_prefix": prefix}
    await state.clear()
    await _panel_log(session, shop, staff, panel, after)
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(panel_text(t, panel), reply_markup=panel_kb(t, panel))
