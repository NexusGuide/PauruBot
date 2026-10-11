"""Finance navigation and confirmed, bounded non-secret payment settings."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Admin, Nav
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.utils import StaffOnly, show
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.core.cards import CARD_PICKS, digits
from paurubot.core.formatting import esc, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import Shop as ShopRow
from paurubot.db.models import ShopSetting
from paurubot.i18n import Texts
from paurubot.payments import engine
from paurubot.payments.providers.card import active_accounts
from paurubot.payments.providers.crypto import PROVIDERS as CRYPTO_PROVIDERS

router = Router(name="admin_finance")
router.callback_query.filter(StaffOnly())
router.message.filter(StaffOnly())


class FinanceStates(StatesGroup):
    value = State()


GATEWAYS = (
    "card",
    "plisio",
    "nowpayments",
    "tronado",
    "tetra98",
    "cubepay",
    "blupal",
    "variza",
    "abangateway",
    "atlaspay",
    "zarinpal",
    "crypto",
    "stars",
    "aqayepardakht",
)
FLAGS = tuple(f"{name}_enabled" for name in GATEWAYS)
BOOLEANS = (
    "card_random_tail",
    "card_verify_enabled",
    "receipt_duplicate_block",
    "card_auto_approve",
    "zarinpal_sandbox",
    "abangateway_rial",
)
BOUNDS = {
    "min_topup": (0, 10**12),
    "max_topup": (1, 10**12),
    "stars_rate": (1, 10**9),
    "payment_expire_minutes": (1, 10080),
    "card_auto_max_amount": (1, 10**12),
    "card_auto_min_history": (1, 1000),
    "card_auto_approve_after_minutes": (0, 10080),
    "card_verify_min_amount": (0, 10**12),
    "tronado_wage_percent": (0, 100),
    "cubepay_fee_percent": (0, 100),
    "fx_markup_percent": (0, 100),
    "usdt_rate": (0, 10**12),
    "trx_rate": (0, 10**12),
    "ton_rate": (0, 10**12),
}
KEYS = (*FLAGS, *BOOLEANS, *BOUNDS, "card_pick", "card_accounts")
CARD_KEYS = (
    "card_random_tail",
    "card_verify_enabled",
    "card_verify_min_amount",
    "receipt_duplicate_block",
    "card_auto_approve",
    "card_auto_max_amount",
    "card_auto_min_history",
    "card_auto_approve_after_minutes",
)
OPTIONS = {
    "stars": ("stars_rate",),
    "zarinpal": ("zarinpal_sandbox",),
    "abangateway": ("abangateway_rial",),
    "tronado": ("tronado_wage_percent",),
    "cubepay": ("cubepay_fee_percent",),
    "crypto": ("fx_markup_percent", "usdt_rate", "trx_rate", "ton_rate"),
}
TTL = 900


def button(
    t: Texts, key: str, section: str, action: str = "show", id: int = 0, page: int = 0
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=t(key), callback_data=Admin(section=section, action=action, id=id, page=page).pack()
    )


def home_kb(t: Texts, staff: StaffIdentity) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if staff.can("reports.read"):
        kb.row(button(t, "adm_finance", "finance"))
    if staff.can("settings.write"):
        kb.row(button(t, "adm_f_gateways", "fgw"))
        kb.row(button(t, "adm_f_card", "fcard"))
        kb.row(button(t, "adm_f_bonus", "fbonus"))
    other = []
    if staff.can("payments.review"):
        other.append(button(t, "adm_payments", "payments"))
    if staff.can("wallet.adjust"):
        other.append(button(t, "adm_gc_btn", "gc"))
    if other:
        kb.row(*other)
    kb.row(button(t, "adm_ud_admin", "home"))
    kb.row(button(t, "adm_ud_close", "fhome", "close"))
    return kb.as_markup()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def masked_account(item: dict[str, Any]) -> str:
    number = digits(str(item.get("number") or ""))
    return f"**** {number[-4:]} · {item.get('bank') or '—'} · {item.get('holder') or '—'}"


def web_button(t: Texts) -> InlineKeyboardButton | None:
    url = get_settings().public_url.rstrip("/")
    return InlineKeyboardButton(text=t("adm_f_secure_web"), url=f"{url}/admin/settings") if url else None


async def ready(name: str, cfg: dict[str, Any]) -> bool:
    proposed = {**cfg, f"{name}_enabled": True}
    if name == "crypto":
        return any([await provider.enabled(proposed) for provider in CRYPTO_PROVIDERS.values()])
    return await engine.PROVIDERS[name].enabled(proposed)


async def status(t: Texts, name: str, cfg: dict[str, Any]) -> str:
    if not cfg.get(f"{name}_enabled"):
        return t("adm_f_off")
    return t("adm_f_on") if await ready(name, cfg) else t("adm_f_incomplete")


def setting_button(t: Texts, key: str, cfg: dict[str, Any]) -> InlineKeyboardButton:
    value = cfg.get(key)
    if key in BOOLEANS:
        shown = t("adm_f_on" if value else "adm_f_off")
        action = "off" if value else "on"
    else:
        shown, action = money(value), "ask"
    return InlineKeyboardButton(
        text=t(f"adm_f_setting_{key}") + ": " + shown,
        callback_data=Admin(section="fset", action=action, id=KEYS.index(key)).pack(),
    )


async def render(
    event: CallbackQuery | Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    section: str,
    id: int = 0,
) -> None:
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    kb = InlineKeyboardBuilder()
    if section == "fgw" and id == -1:
        kb.row(
            *[
                InlineKeyboardButton(text=t(key), callback_data=Nav(to="noop").pack())
                for key in ("adm_f_operations", "adm_f_status", "adm_f_title")
            ]
        )
        for index, name in enumerate(GATEWAYS):
            target = Admin(section="fgw", action="view", id=index).pack()
            kb.row(
                InlineKeyboardButton(text=t("adm_f_settings"), callback_data=target),
                InlineKeyboardButton(text=await status(t, name, cfg), callback_data=target),
                InlineKeyboardButton(text=t(f"adm_f_gateway_{name}"), callback_data=target),
            )
        kb.row(setting_button(t, "max_topup", cfg), setting_button(t, "min_topup", cfg))
        kb.row(button(t, "adm_f_bonus", "fbonus"))
        text = t("adm_f_gateways_head")
    elif section == "fgw":
        name = GATEWAYS[id]
        available = await ready(name, cfg)
        kb.row(
            button(
                t,
                "adm_f_disable" if cfg.get(f"{name}_enabled") else "adm_f_enable",
                "fset",
                "off" if cfg.get(f"{name}_enabled") else "on",
                KEYS.index(f"{name}_enabled"),
            )
        )
        for key in OPTIONS.get(name, ()):
            kb.row(setting_button(t, key, cfg))
        if name == "card":
            kb.row(button(t, "adm_f_card", "fcard"))
        kb.row(button(t, "adm_f_bonus", "fbonus"))
        text = t(
            "adm_f_gateway_head",
            name=esc(t(f"adm_f_gateway_{name}")),
            state=await status(t, name, cfg),
            readiness=t("adm_f_ready" if available else "adm_f_not_ready"),
        )
        kb.row(button(t, "adm_f_back_gateways", "fgw"))
    elif section == "fcard":
        kb.row(button(t, "adm_f_accounts", "faccounts"))
        for key in CARD_KEYS:
            kb.row(setting_button(t, key, cfg))
        kb.row(setting_button(t, "max_topup", cfg), setting_button(t, "min_topup", cfg))
        kb.row(setting_button(t, "payment_expire_minutes", cfg))
        for index, choice in enumerate(CARD_PICKS):
            kb.row(
                InlineKeyboardButton(
                    text=("✅ " if cfg.get("card_pick") == choice else "") + t(f"adm_f_pick_{choice}"),
                    callback_data=Admin(
                        section="fset", action="pick", id=KEYS.index("card_pick"), page=index
                    ).pack(),
                )
            )
        kb.row(
            button(
                t,
                "adm_f_card_enable" if not cfg.get("card_enabled") else "adm_f_card_disable",
                "fset",
                "on" if not cfg.get("card_enabled") else "off",
                KEYS.index("card_enabled"),
            )
        )
        kb.row(button(t, "adm_f_bonus", "fbonus"))
        text = t("adm_f_card_head", state=await status(t, "card", cfg), accounts=len(active_accounts(cfg)))
    elif section == "faccounts":
        accounts = cfg.get("card_accounts") or []
        lines = [t("adm_f_accounts_head")]
        for index, item in enumerate(accounts[:10]):
            if not isinstance(item, dict):
                continue
            lines.append(f"{index + 1}. {esc(masked_account(item))}")
            kb.row(
                InlineKeyboardButton(
                    text=(t("adm_f_off") if item.get("disabled") else t("adm_f_on")) + f" #{index + 1}",
                    callback_data=Admin(
                        section="fset", action="acc_on" if item.get("disabled") else "acc_off", id=index
                    ).pack(),
                )
            )
        if not accounts:
            lines.append(t("adm_cards_empty"))
        text = "\n".join(lines)
        kb.row(button(t, "btn_back", "fcard"))
    else:  # read-only top-up bonus rules; edit the validated structured value in web settings
        lines = [t("adm_f_bonus_head")]
        for index, rule in enumerate((cfg.get("topup_bonus_rules") or [])[:20]):
            if not isinstance(rule, dict):
                continue
            lines.append(
                t(
                    "adm_f_bonus_line",
                    id=index + 1,
                    provider=esc(rule.get("provider") or "*"),
                    percent=esc(rule.get("percent") or 0),
                    minimum=money(rule.get("min_amount")),
                    cap=money(rule.get("max_bonus")) if rule.get("max_bonus") else t("unlimited"),
                )
            )
        if len(lines) == 1:
            lines.append(t("adm_f_bonus_empty"))
        text = "\n\n".join(lines)
    secure = web_button(t)
    if secure:
        kb.row(secure)
    else:
        text += "\n\n" + t("adm_f_web_missing")
    kb.row(button(t, "adm_f_back_home", "fhome"), button(t, "adm_ud_admin", "home"))
    kb.row(button(t, "adm_ud_close", "fhome", "close"))
    await show(event, text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "fhome"))
async def home(
    call: CallbackQuery, callback_data: Admin, t: Texts, staff: StaffIdentity, state: FSMContext
) -> None:
    if not any(staff.can(p) for p in ("reports.read", "settings.write", "payments.review", "wallet.adjust")):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await show(
        call,
        t("adm_f_closed") if callback_data.action == "close" else t("adm_f_home"),
        None if callback_data.action == "close" else home_kb(t, staff),
    )


@router.callback_query(
    Admin.filter(F.section.in_({"fgw", "fcard", "faccounts", "fbonus"})), StaffOnly("settings.write")
)
async def pages(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    if (
        callback_data.section == "fgw"
        and callback_data.action == "view"
        and not 0 <= callback_data.id < len(GATEWAYS)
    ):
        await call.answer(t("button_expired"), show_alert=True)
        return
    if callback_data.action not in {"show", "view"}:
        await call.answer(t("button_expired"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await render(
        call,
        session,
        shop,
        t,
        staff,
        callback_data.section,
        callback_data.id if callback_data.section == "fgw" and callback_data.action == "view" else -1,
    )


def return_to(key: str) -> tuple[str, int]:
    if key == "card_accounts":
        return "faccounts", -1
    if key.startswith("card_") or key == "receipt_duplicate_block":
        return "fcard", -1
    for name in GATEWAYS:
        if key.startswith(name + "_"):
            return "fgw", GATEWAYS.index(name)
    return "fgw", -1


async def validate(key: str, value: Any, cfg: dict[str, Any]) -> bool:
    proposed = {**cfg, key: value}
    if key in BOUNDS:
        low, high = BOUNDS[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            return False
    if (key in FLAGS or key == "card_auto_approve") and value is False:
        return True
    if key in {"min_topup", "max_topup"} and int(proposed.get("min_topup") or 0) > int(
        proposed.get("max_topup") or 0
    ):
        return False
    if key.startswith("card_auto_") and (
        proposed.get("card_auto_approve") or int(proposed.get("card_auto_approve_after_minutes") or 0) > 0
    ):
        if int(proposed.get("card_auto_max_amount") or 0) <= 0:
            return False
    if (
        key.startswith("card_auto_")
        and proposed.get("card_auto_approve")
        and int(proposed.get("card_auto_min_history") or 0) < 1
    ):
        return False
    if key in FLAGS and value is True:
        return await ready(key.removesuffix("_enabled"), proposed)
    return True


async def preview(
    event: CallbackQuery | Message,
    key: str,
    value: Any,
    cfg: dict[str, Any],
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
    *,
    account_index: int | None = None,
) -> None:
    nonce = secrets.randbelow(2**31 - 1) + 1
    pending = {
        "key": key,
        "value": value,
        "shop_id": shop.id,
        "nonce": nonce,
        "at": utcnow().timestamp(),
        "before": digest(cfg.get(key)),
        "account_index": account_index,
    }
    await state.set_state(None)
    await state.set_data({"finance_pending": pending})
    if account_index is None:
        title = (
            t(f"adm_f_setting_{key}")
            if key not in FLAGS
            else t(f"adm_f_gateway_{key.removesuffix('_enabled')}")
        )
        before, after = cfg.get(key), value

        def shown(v: Any) -> str:
            return t("adm_f_on" if v else "adm_f_off") if isinstance(v, bool) else esc(v)

        text = t("adm_f_confirm", title=esc(title), before=shown(before), after=shown(after))
    else:
        text = t(
            "adm_f_account_confirm",
            card=esc(masked_account(cfg["card_accounts"][account_index])),
            state=t("adm_f_on" if value else "adm_f_off"),
        )
    if key in {
        "card_auto_approve",
        "card_auto_approve_after_minutes",
        "card_auto_max_amount",
        "card_auto_min_history",
    }:
        text += "\n\n" + t("adm_f_auto_warning")
    target = Admin(section="fset", action="confirm", id=KEYS.index(key), page=nonce)
    section, id = return_to(key)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [button(t, "adm_f_apply", "fset", "confirm", target.id, nonce)],
            [
                button(
                    t, "btn_cancel", section, "view" if section == "fgw" and id >= 0 else "show", max(id, 0)
                )
            ],
        ]
    )
    await show(event, text, kb)


async def locked_setting(session: AsyncSession, shop_id: int, key: str) -> None:
    # Lock the actual setting row, including previously implicit defaults. Web/API upserts use
    # this same row and cannot slip a write between the fresh comparison and the commit.
    stmt = (
        insert(ShopSetting)
        .values(shop_id=shop_id, key=key, value=shop_settings.SPECS[key].default)
        .on_conflict_do_nothing(index_elements=[ShopSetting.shop_id, ShopSetting.key])
    )
    await session.execute(stmt)
    await session.execute(
        select(ShopSetting).where(ShopSetting.shop_id == shop_id, ShopSetting.key == key).with_for_update()
    )


@router.callback_query(Admin.filter(F.section == "fset"), StaffOnly("settings.write"))
async def settings(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action in {"acc_on", "acc_off"}:
        key = "card_accounts"
    elif not 0 <= callback_data.id < len(KEYS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    else:
        key = KEYS[callback_data.id]
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if action == "confirm":
        pending = (await state.get_data()).get("finance_pending")
        if (
            not isinstance(pending, dict)
            or pending.get("shop_id") != shop.id
            or pending.get("key") != key
            or pending.get("nonce") != callback_data.page
            or utcnow().timestamp() - float(pending.get("at") or 0) > TTL
        ):
            await call.answer(t("button_expired"), show_alert=True)
            return
        related = {key}
        if key in {"min_topup", "max_topup"}:
            related |= {"min_topup", "max_topup"}
        if key.startswith("card_auto_"):
            related |= {
                "card_auto_approve",
                "card_auto_max_amount",
                "card_auto_min_history",
                "card_auto_approve_after_minutes",
            }
        for locked_key in sorted(related):
            await locked_setting(session, shop.id, locked_key)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        if digest(cfg.get(key)) != pending.get("before"):
            await state.clear()
            await call.answer(t("adm_f_changed"), show_alert=True)
            return
        value = pending.get("value")
        account_index = pending.get("account_index")
        if account_index is not None:
            accounts = cfg.get("card_accounts") or []
            if (
                not isinstance(account_index, int)
                or not 0 <= account_index < len(accounts)
                or not isinstance(accounts[account_index], dict)
            ):
                await call.answer(t("button_expired"), show_alert=True)
                return
            updated = [dict(item) for item in accounts]
            updated[account_index]["disabled"] = not bool(value)
            before_audit = {
                "card": masked_account(accounts[account_index]),
                "enabled": not accounts[account_index].get("disabled", False),
            }
            after_audit = {"card": masked_account(updated[account_index]), "enabled": bool(value)}
            value = updated
        else:
            if not await validate(key, value, cfg):
                await state.clear()
                await call.answer(t("adm_f_invalid_or_not_ready"), show_alert=True)
                return
            before_audit, after_audit = {key: cfg.get(key)}, {key: value}
        await state.clear()  # consume this session's confirmation; row lock + digest handles races
        if cfg.get(key) != value:
            await shop_settings.set_many(session, shop.id, {key: value})
            await record(
                session,
                shop_id=shop.id,
                actor=actor(staff),
                action="settings.finance_update",
                entity="shop",
                entity_id=shop.id,
                before=before_audit,
                after=after_audit,
            )
        await session.commit()
        await call.answer(t("adm_saved"))
        section, id = return_to(key)
        await render(call, session, shop, t, staff, section, id)
        return
    if action == "ask" and key in BOUNDS:
        await state.clear()
        await state.set_state(FinanceStates.value)
        await state.update_data(finance_key=key, finance_shop_id=shop.id)
        low, high = BOUNDS[key]
        await call.answer()
        await show(
            call,
            t("adm_f_ask_number", name=esc(t(f"adm_f_setting_{key}")), low=money(low), high=money(high)),
            InlineKeyboardMarkup(inline_keyboard=[[button(t, "btn_cancel", "fhome")]]),
        )
        return
    account_index = None
    if action in {"on", "off"} and key in (*FLAGS, *BOOLEANS):
        value = action == "on"
    elif action == "pick" and key == "card_pick" and 0 <= callback_data.page < len(CARD_PICKS):
        value = CARD_PICKS[callback_data.page]
    elif action in {"acc_on", "acc_off"}:
        accounts = cfg.get(key) or []
        account_index = callback_data.id
        if not 0 <= account_index < min(len(accounts), 10) or not isinstance(accounts[account_index], dict):
            await call.answer(t("button_expired"), show_alert=True)
            return
        value = action == "acc_on"
    else:
        await call.answer(t("button_expired"), show_alert=True)
        return
    if account_index is None and not await validate(key, value, cfg):
        await call.answer(t("adm_f_invalid_or_not_ready"), show_alert=True)
        return
    await call.answer()
    await preview(call, key, value, cfg, shop, t, state, account_index=account_index)


@router.message(FinanceStates.value, F.text)
async def number_input(
    message: Message, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity, state: FSMContext
) -> None:
    data = await state.get_data()
    key = data.get("finance_key")
    if not staff.can("settings.write") or data.get("finance_shop_id") != shop.id or key not in BOUNDS:
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    value = parse_int(message.text)
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if value is None or not await validate(key, value, cfg):
        await message.answer(t("adm_f_invalid_or_not_ready"))
        return
    await preview(message, key, value, cfg, shop, t, state)
