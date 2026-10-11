"""Lucky wheel, lotteries and the trust channel from the bot (ADM-14, GRW-03, GRW-04, GRW-08)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_config import _ask, _back, _btn, save, shown
from paurubot.bot.routers.admin_extra import extra_buttons
from paurubot.bot.routers.start import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import growth
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import Lottery
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="admin_growth")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

SECTION = "grw"
PERMISSION = "settings.write"
TOGGLES = ("wheel_enabled", "wheel_buyers_only", "lottery_enabled")
ASKS = ("wheel_every_hours", "trust_channel_id")
MAX_PRIZES = 12
MIN_PRIZES = 2
LOT_PAGE = 10
MODES = ("join", "purchases")
CHAT_ID_RE = re.compile(r"-?\d{5,20}")


def growth_buttons(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    """Hub entries for this section and the extra settings (admin_extra)."""
    if not staff.can(PERMISSION):
        return []
    return [(t("adm_grw_btn"), Admin(section=SECTION)), *extra_buttons(t, staff)]


# --- parsing ---------------------------------------------------------------------------------


def parse_ask(key: str, raw: str) -> tuple[Any, bool]:
    """(value, ok) for the typed wheel interval or trust channel id; "-" clears."""
    raw = raw.strip()
    if key == "trust_channel_id":
        if raw == "-":
            return None, True
        return (int(raw), True) if CHAT_ID_RE.fullmatch(raw) else (None, False)
    if raw == "-":
        return shop_settings.SPECS[key].default, True
    number = parse_int(raw)
    if number is None or not 1 <= number <= 24 * 30:
        return None, False
    return number, True


def parse_prize(text: str) -> dict[str, Any] | None:
    """`label | wallet|discount|nothing | value | weight` -> one validated wheel slice."""
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 4:
        return None
    label, kind, raw_value, raw_weight = parts
    value, weight = parse_int(raw_value), parse_int(raw_weight)
    if value is None or weight is None:
        return None
    item = {"label": label, "kind": kind.lower(), "value": value, "weight": weight}
    try:
        # validate_prizes needs two slices; a zero-weight twin keeps the check about this one.
        twin = {"label": "x", "kind": "nothing", "value": 0, "weight": 0}
        clean = growth.validate_prizes([item, twin])
    except growth.GrowthError:
        return None
    if clean[0]["weight"] <= 0:
        return None
    return clean[0]


def parse_lottery(text: str) -> dict[str, Any] | None:
    """`title | prize | wallet amount | winners | join|purchases | days`."""
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 6:
        return None
    title, prize, raw_amount, raw_winners, mode, raw_days = parts
    amount, winners, days = parse_int(raw_amount), parse_int(raw_winners), parse_int(raw_days)
    mode = mode.lower()
    if not title or len(title) > 96 or not prize or len(prize) > 255 or mode not in MODES:
        return None
    if amount is None or not 0 <= amount <= 10**10:
        return None
    if winners is None or not 1 <= winners <= 100 or days is None or not 1 <= days <= 365:
        return None
    return {
        "title": title,
        "prize": prize,
        "prize_amount": amount,
        "winners": winners,
        "mode": mode,
        "days": days,
    }


# --- views -----------------------------------------------------------------------------------


def home_kb(t: Texts, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, key in enumerate(TOGGLES):
        name = t(f"adm_grw_{key}")
        label = f"{name}: {shown(key, cfg.get(key))}"
        kb.row(_btn(label, Admin(section=SECTION, action="tog", id=index)))
    for index, key in enumerate(ASKS):
        name = t(f"adm_grw_{key}")
        label = f"{name}: {shown(key, cfg.get(key))}"
        kb.row(_btn(label, Admin(section=SECTION, action="ask", id=index)))
    kb.row(_btn(t("adm_grw_prizes_btn"), Admin(section=SECTION, action="prizes")))
    kb.row(_btn(t("adm_grw_lots_btn"), Admin(section=SECTION, action="lots")))
    kb.row(_back(t, Admin(section="home")))
    return kb.as_markup()


def current_prizes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return growth.validate_prizes(cfg.get("wheel_prizes") or [])
    except growth.GrowthError:
        return []


def prize_line(t: Texts, prize: dict[str, Any]) -> str:
    kind = t(f"adm_grw_kind_{prize['kind']}")
    return t(
        "adm_grw_prize_line",
        label=esc(prize["label"]),
        kind=kind,
        value=prize["value"],
        weight=prize["weight"],
    )


def prizes_view(t: Texts, cfg: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    prizes = current_prizes(cfg)
    total = sum(p["weight"] for p in prizes) or 1
    lines = [t("adm_grw_prizes_title")]
    kb = InlineKeyboardBuilder()
    for index, prize in enumerate(prizes):
        chance = round(prize["weight"] * 100 / total, 1)
        lines.append(f"{index + 1}. {prize_line(t, prize)} ({chance}%)")
        target = Admin(section=SECTION, action="pdel", id=index)
        kb.row(_btn(t("adm_grw_prize_del", n=index + 1), target))
    if len(prizes) < MAX_PRIZES:
        kb.row(_btn(t("adm_grw_prize_add"), Admin(section=SECTION, action="padd")))
    kb.row(_btn(t("adm_grw_prize_reset"), Admin(section=SECTION, action="preset")))
    kb.row(_back(t, Admin(section=SECTION)))
    return "\n".join(lines), kb.as_markup()


def lottery_text(t: Texts, lot: Lottery) -> str:
    lines = [
        t("adm_grw_lot_head", id=lot.id, title=esc(lot.title), status=t(f"adm_grw_status_{lot.status}")),
        t("adm_grw_lot_prize", prize=esc(lot.prize), amount=money(lot.prize_amount), winners=lot.winners),
        t(
            "adm_grw_lot_when",
            mode=t(f"adm_grw_mode_{lot.mode}"),
            start=jdate(lot.starts_at, with_time=True),
            end=jdate(lot.ends_at, with_time=True),
        ),
    ]
    result = lot.result or {}
    if lot.status == "drawn":
        lines.append(
            t(
                "adm_grw_lot_result",
                entrants=int(result.get("entrants") or 0),
                winners=", ".join(str(x) for x in result.get("winners", [])) or "\u2014",
            )
        )
    return "\n".join(lines)


def lottery_kb(t: Texts, lot: Lottery) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if lot.status == "open":
        kb.row(_btn(t("adm_grw_lot_end"), Admin(section=SECTION, action="lend", id=lot.id)))
        kb.row(_btn(t("adm_grw_lot_cancel"), Admin(section=SECTION, action="lcancel", id=lot.id)))
    kb.row(_back(t, Admin(section=SECTION, action="lots")))
    return kb.as_markup()


async def lotteries_view(session: AsyncSession, shop_id: int, t: Texts) -> tuple[str, InlineKeyboardMarkup]:
    rows = list(
        await session.scalars(
            select(Lottery).where(Lottery.shop_id == shop_id).order_by(Lottery.id.desc()).limit(LOT_PAGE)
        )
    )
    kb = InlineKeyboardBuilder()
    for lot in rows:
        status = t(f"adm_grw_status_{lot.status}")
        label = f"#{lot.id} {lot.title} \u00b7 {status}"
        kb.row(_btn(label, Admin(section=SECTION, action="lv", id=lot.id)))
    kb.row(_btn(t("adm_grw_lot_new"), Admin(section=SECTION, action="lnew")))
    kb.row(_back(t, Admin(section=SECTION)))
    return t("adm_grw_lots_title") if rows else t("adm_grw_lots_empty"), kb.as_markup()


async def _own_lottery(session: AsyncSession, shop_id: int, lottery_id: int) -> Lottery | None:
    lot = await session.get(Lottery, lottery_id)
    return lot if lot is not None and lot.shop_id == shop_id else None


async def _lottery_log(
    session: AsyncSession, shop: ShopRow, staff: StaffIdentity, action: str, lot: Lottery, before: Any
) -> None:
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=f"lottery.{action}",
        entity="lottery",
        entity_id=lot.id,
        before=before,
        after={"status": lot.status, "ends_at": lot.ends_at.isoformat()},
    )


# --- handlers --------------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == SECTION), StaffOnly(PERMISSION))
async def growth_admin(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if action == "tog":
        if not 0 <= callback_data.id < len(TOGGLES):
            await call.answer(t("button_expired"), show_alert=True)
            return
        key = TOGGLES[callback_data.id]
        value = not bool(cfg.get(key))
        if value and key == "wheel_enabled" and not current_prizes(cfg):
            await call.answer(t("adm_grw_prizes_needed"), show_alert=True)
            return
        await save(session, shop, staff, key, value)
        await call.answer(t("adm_saved"))
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, t("adm_grw_title"), home_kb(t, cfg))
        return
    if action == "ask":
        if not 0 <= callback_data.id < len(ASKS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        key = ASKS[callback_data.id]
        await call.answer()
        await state.set_state(AdminStates.growth_value)
        await state.update_data(growth_key=key)
        name = esc(t(f"adm_grw_{key}"))
        await _ask(call, t("adm_grw_ask_value", name=name, value=esc(shown(key, cfg.get(key)))), t)
        return
    if action == "prizes":
        await call.answer()
        text, kb = prizes_view(t, cfg)
        await show(call, text, kb)
        return
    if action == "pdel":
        prizes = current_prizes(cfg)
        if not 0 <= callback_data.id < len(prizes):
            await call.answer(t("button_expired"), show_alert=True)
            return
        rest = [p for i, p in enumerate(prizes) if i != callback_data.id]
        try:
            rest = growth.validate_prizes(rest)
        except growth.GrowthError:
            await call.answer(t("adm_grw_prize_min", n=MIN_PRIZES), show_alert=True)
            return
        await save(session, shop, staff, "wheel_prizes", rest)
        await call.answer(t("adm_saved"))
        text, kb = prizes_view(t, await shop_settings.load(session, shop.id, fresh=True))
        await show(call, text, kb)
        return
    if action == "preset":
        await save(session, shop, staff, "wheel_prizes", list(shop_settings.SPECS["wheel_prizes"].default))
        await call.answer(t("adm_saved"))
        text, kb = prizes_view(t, await shop_settings.load(session, shop.id, fresh=True))
        await show(call, text, kb)
        return
    if action == "padd":
        if len(current_prizes(cfg)) >= MAX_PRIZES:
            await call.answer(t("adm_grw_prize_max", n=MAX_PRIZES), show_alert=True)
            return
        await call.answer()
        await state.set_state(AdminStates.prize_new)
        await _ask(call, t("adm_grw_prize_ask"), t)
        return
    if action == "lots":
        await call.answer()
        text, kb = await lotteries_view(session, shop.id, t)
        await show(call, text, kb)
        return
    if action == "lnew":
        await call.answer()
        await state.set_state(AdminStates.lottery_new)
        await _ask(call, t("adm_grw_lot_ask"), t)
        return
    if action in {"lv", "lend", "lcancel"}:
        lot = await _own_lottery(session, shop.id, callback_data.id)
        if lot is None:
            await call.answer(t("button_expired"), show_alert=True)
            return
        if action != "lv":
            if lot.status != "open":
                await call.answer(t("adm_grw_lot_not_open"), show_alert=True)
                return
            before = {"status": lot.status, "ends_at": lot.ends_at.isoformat()}
            if action == "lend":
                # The draw worker picks it up on its next run and notifies the winners.
                lot.ends_at = min(lot.ends_at, utcnow())
                note = "adm_grw_lot_ended"
            else:
                lot.status = "canceled"
                note = "adm_grw_lot_canceled"
            await _lottery_log(session, shop, staff, "end" if action == "lend" else "cancel", lot, before)
            await session.commit()
            await call.answer(t(note), show_alert=True)
        else:
            await call.answer()
        await show(call, lottery_text(t, lot), lottery_kb(t, lot))
        return
    await call.answer()
    await state.clear()
    await show(call, t("adm_grw_title"), home_kb(t, cfg))


@router.message(AdminStates.growth_value, F.text)
async def growth_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    key = str(data.get("growth_key") or "")
    if key not in ASKS or not staff.can(PERMISSION):
        await state.clear()
        return
    value, ok = parse_ask(key, message.text or "")
    if not ok:
        await message.answer(t("adm_grw_bad_value"))
        return
    await state.clear()
    await save(session, shop, staff, key, value)
    fresh = await shop_settings.load(session, shop.id, fresh=True)
    await message.answer(t("adm_saved"), reply_markup=menu(t, fresh, staff))
    await message.answer(t("adm_grw_title"), reply_markup=home_kb(t, fresh))


@router.message(AdminStates.prize_new, F.text)
async def prize_new(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    if not staff.can(PERMISSION):
        await state.clear()
        return
    prize = parse_prize(message.text or "")
    if prize is None:
        await message.answer(t("adm_grw_prize_bad"))
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    prizes = current_prizes(cfg)
    if len(prizes) >= MAX_PRIZES:
        await state.clear()
        await message.answer(t("adm_grw_prize_max", n=MAX_PRIZES), reply_markup=menu(t, cfg, staff))
        return
    prizes.append(prize)
    await state.clear()
    await save(session, shop, staff, "wheel_prizes", prizes)
    fresh = await shop_settings.load(session, shop.id, fresh=True)
    await message.answer(t("adm_saved"), reply_markup=menu(t, fresh, staff))
    text, kb = prizes_view(t, fresh)
    await message.answer(text, reply_markup=kb)


@router.message(AdminStates.lottery_new, F.text)
async def lottery_new(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not staff.can(PERMISSION):
        await state.clear()
        return
    spec = parse_lottery(message.text or "")
    if spec is None:
        await message.answer(t("adm_grw_lot_bad"))
        return
    now = utcnow()
    lot = Lottery(
        shop_id=shop.id,
        title=spec["title"],
        prize=spec["prize"],
        prize_amount=spec["prize_amount"],
        winners=spec["winners"],
        mode=spec["mode"],
        starts_at=now,
        ends_at=now + timedelta(days=spec["days"]),
        status="open",
        result={},
    )
    session.add(lot)
    await session.flush()
    await _lottery_log(session, shop, staff, "create", lot, None)
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    if not cfg.get("lottery_enabled"):
        await message.answer(t("adm_grw_lot_disabled_hint"))
    await message.answer(lottery_text(t, lot), reply_markup=lottery_kb(t, lot))
