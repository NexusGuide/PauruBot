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
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import growth
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, jdate, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Lottery
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.notifications import log_reports

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
    """`title | mode | pick | days [| toman per point [| shown]]`; prizes are added afterwards."""
    parts = [part.strip() for part in text.split("|")]
    if not 4 <= len(parts) <= 6:
        return None
    title, mode, pick, raw_days = parts[0], parts[1].lower(), parts[2].lower(), parts[3]
    raw_per = parts[4] if len(parts) > 4 else "-"
    board = parts[5].lower() if len(parts) > 5 else "none"
    days = parse_int(raw_days)
    per = 0 if raw_per in {"", "-"} else parse_int(raw_per)
    if not title or len(title) > 96 or mode not in growth.LOTTERY_MODES:
        return None
    if pick not in growth.LOTTERY_PICKS or board not in growth.LOTTERY_BOARDS:
        return None
    if days is None or not 1 <= days <= 365 or per is None or not 0 <= per <= 10**9:
        return None
    if mode == "points" and per < 1:
        return None
    return {
        "title": title,
        "mode": mode,
        "pick": pick,
        "days": days,
        "points_per": per if mode == "points" else 0,
        "board": board,
    }


def parse_lottery_prize(t: Texts, text: str) -> dict[str, Any] | None:
    """`gb 10`, `days 5`, `wallet 10000` or `text a special account`, in either language;
    anything after `|` is a label for the prize."""
    main, _, label = text.partition("|")
    words = main.split()
    if not words:
        return None
    first = words[0].lower()
    kinds = {
        kind: {word.strip().lower() for word in t(f"adm_grw_lot_kw_{kind}").split(",")} | {kind}
        for kind in growth.LOTTERY_PRIZE_KINDS
    }
    if first in kinds["text"]:
        item: dict[str, Any] = {"kind": "text", "value": 0, "label": " ".join(words[1:]) or label.strip()}
    else:
        kind = next((k for k, names in kinds.items() if k != "text" and first in names), None)
        value = parse_int(words[1]) if len(words) == 2 else None
        if kind is None or value is None:
            return None
        item = {"kind": kind, "value": value, "label": label.strip()}
    try:
        return growth.validate_lottery_prizes([item])[0]
    except growth.GrowthError:
        return None


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


async def lottery_text(session: AsyncSession, t: Texts, lot: Lottery) -> str:
    lines = [
        t("adm_grw_lot_head", id=lot.id, title=esc(lot.title), status=t(f"adm_grw_status_{lot.status}")),
        t(
            "adm_grw_lot_how",
            mode=log_reports.mode_text(t, lot),
            pick=t(f"adm_grw_pick_{lot.pick}"),
            board=t(f"adm_grw_board_{lot.board}"),
        ),
        t(
            "adm_grw_lot_when",
            start=jdate(lot.starts_at, with_time=True),
            end=jdate(lot.ends_at, with_time=True),
        ),
    ]
    if lot.status == "drawn":
        result = lot.result or {}
        lines.append(
            t(
                "adm_grw_lot_counts",
                entrants=int(result.get("entrants") or 0),
                tickets=int(result.get("tickets") or 0),
            )
        )
        lines.append("")
        lines.append(t("adm_grw_lot_awards_title"))
        for award in log_reports.lottery_awards(lot):
            customer = await session.get(Customer, int(award["customer_id"]))
            lines.append(
                t(
                    "adm_grw_lot_award_row",
                    mark=log_reports.rank_mark(int(award["rank"])),
                    rank=t("lot_rank", rank=award["rank"]),
                    name=esc(log_reports.person_name(customer)) if customer else f"#{award['customer_id']}",
                    prize=log_reports.prize_text(t, award),
                    status=log_reports.award_status(t, award),
                )
            )
        return "\n".join(lines)
    board = await growth.standings(session, lot)
    lines.append(t("adm_grw_lot_counts", entrants=len(board), tickets=sum(r.points for r in board)))
    lines.append("")
    prizes = growth.prize_list(lot)
    if not prizes:
        lines.append(t("adm_grw_lot_no_prizes"))
    else:
        lines.append(t("adm_grw_lot_prizes_title"))
        for rank, prize in enumerate(prizes, start=1):
            lines.append(
                t(
                    "adm_grw_lot_prize_row",
                    mark=log_reports.rank_mark(rank),
                    rank=t("lot_rank", rank=rank),
                    prize=log_reports.prize_text(t, prize),
                )
            )
    return "\n".join(lines)


def lottery_kb(t: Texts, lot: Lottery) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(t("adm_grw_lot_board_btn"), Admin(section=SECTION, action="lbd", id=lot.id)))
    if lot.status == "open":
        prizes = growth.prize_list(lot)
        if len(prizes) < growth.MAX_LOTTERY_PRIZES:
            kb.row(_btn(t("adm_grw_lot_prize_add"), Admin(section=SECTION, action="lpadd", id=lot.id)))
        dels = [
            _btn(
                t("adm_grw_lot_prize_del", rank=index + 1),
                Admin(section=SECTION, action="lpdel", id=lot.id, page=index),
            )
            for index in range(len(prizes))
        ]
        for index in range(0, len(dels), 3):
            kb.row(*dels[index : index + 3])
        kb.row(_btn(t("adm_grw_lot_end"), Admin(section=SECTION, action="lend", id=lot.id)))
        kb.row(_btn(t("adm_grw_lot_cancel"), Admin(section=SECTION, action="lcancel", id=lot.id)))
    kb.row(_back(t, Admin(section=SECTION, action="lots")))
    return kb.as_markup()


async def board_view(
    session: AsyncSession, t: Texts, lot: Lottery, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """The standings, best first, ten a page: who they are and their score."""
    board = await growth.standings(session, lot)
    pages = max(1, -(-len(board) // LOT_PAGE))
    page = min(max(0, page), pages - 1)
    lines = [
        t(
            "adm_grw_lot_board_title",
            title=esc(lot.title),
            mode=log_reports.mode_text(t, lot),
            total=len(board),
        )
    ]
    if not board:
        lines.append(t("adm_grw_lot_board_empty"))
    for rank, row in enumerate(board[page * LOT_PAGE : (page + 1) * LOT_PAGE], start=page * LOT_PAGE + 1):
        customer = await session.get(Customer, row.customer_id)
        if customer is None:
            continue
        lines.append(
            t(
                "adm_grw_lot_board_row",
                mark=log_reports.rank_mark(rank) if rank <= 3 else "",
                rank=rank,
                name=esc(log_reports.person_name(customer)),
                username=f"@{esc(customer.username)}" if customer.username and customer.first_name else "",
                telegram_id=customer.telegram_id,
                score=log_reports.score_text(t, lot.mode, row.points, row.purchases, row.spent),
            )
        )
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(_btn(t("btn_prev"), Admin(section=SECTION, action="lbd", id=lot.id, page=page - 1)))
    if page < pages - 1:
        nav.append(_btn(t("btn_next"), Admin(section=SECTION, action="lbd", id=lot.id, page=page + 1)))
    if nav:
        kb.row(*nav)
    kb.row(_back(t, Admin(section=SECTION, action="lv", id=lot.id)))
    return "\n\n".join([lines[0], "\n".join(lines[1:])]), kb.as_markup()


def prizes_done_kb(t: Texts, lot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(t("adm_grw_lot_prizes_done"), Admin(section=SECTION, action="lv", id=lot_id)))
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
        after={"status": lot.status, "ends_at": lot.ends_at.isoformat(), "prizes": lot.prizes},
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
        prompt = t("adm_grw_ask_value", name=name, value=esc(shown(key, cfg.get(key))))
        if key == "trust_channel_id":
            prompt = f"{prompt}\n\n{t('adm_grw_trust_hint')}"
        await _ask(call, prompt, t)
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
    if action in {"lv", "lend", "lcancel", "lbd", "lpadd", "lpdel"}:
        lot = await _own_lottery(session, shop.id, callback_data.id)
        if lot is None:
            await call.answer(t("button_expired"), show_alert=True)
            return
        if action == "lbd":
            await call.answer()
            text, kb = await board_view(session, t, lot, callback_data.page)
            await show(call, text, kb)
            return
        if action != "lv" and lot.status != "open":
            await call.answer(t("adm_grw_lot_not_open"), show_alert=True)
            return
        if action == "lpadd":
            if len(growth.prize_list(lot)) >= growth.MAX_LOTTERY_PRIZES:
                await call.answer(t("adm_grw_lot_prize_max", n=growth.MAX_LOTTERY_PRIZES), show_alert=True)
                return
            await call.answer()
            await state.set_state(AdminStates.lottery_prize)
            await state.update_data(lottery_id=lot.id)
            rank = t("lot_rank", rank=len(growth.prize_list(lot)) + 1)
            await show(call, t("adm_grw_lot_prize_ask", rank=rank), prizes_done_kb(t, lot.id))
            return
        if action == "lpdel":
            prizes = growth.prize_list(lot)  # a lottery from before ranked prizes keeps its own
            if not 0 <= callback_data.page < len(prizes):
                await call.answer(t("button_expired"), show_alert=True)
                return
            lot.prizes = [p for i, p in enumerate(prizes) if i != callback_data.page]
            lot.winners, lot.prize, lot.prize_amount = len(lot.prizes), "", 0
            await _lottery_log(session, shop, staff, "prizes", lot, {"prizes": prizes})
            await session.commit()
            await call.answer(t("adm_saved"))
        elif action in {"lend", "lcancel"}:
            if action == "lend" and not growth.prize_list(lot):
                await call.answer(t("adm_grw_lot_need_prizes"), show_alert=True)
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
            await state.clear()
            await call.answer()
        await show(call, await lottery_text(session, t, lot), lottery_kb(t, lot))
        return
    await call.answer()
    await state.clear()
    await show(call, t("adm_grw_title"), home_kb(t, cfg))


async def sync_trust_rows(session: AsyncSession, shop_id: int, chat_id: int | None) -> None:
    """The bot's trust-channel field stands for the whole list: one channel with purchases and
    renewals, or none. The admin panel's channels page manages several with their own kinds."""
    from paurubot.db.models import TrustChannel

    for row in await session.scalars(select(TrustChannel).where(TrustChannel.shop_id == shop_id)):
        await session.delete(row)
    if chat_id:
        session.add(TrustChannel(shop_id=shop_id, chat_id=int(chat_id), kinds=["purchase", "renew"]))
    await session.flush()


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
    if key == "trust_channel_id":
        await sync_trust_rows(session, shop.id, value)
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
        prize="",
        prize_amount=0,
        winners=0,
        mode=spec["mode"],
        pick=spec["pick"],
        points_per=spec["points_per"],
        board=spec["board"],
        prizes=[],
        starts_at=now,
        ends_at=now + timedelta(days=spec["days"]),
        status="open",
        result={},
    )
    session.add(lot)
    await session.flush()
    await _lottery_log(session, shop, staff, "create", lot, None)
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    if not cfg.get("lottery_enabled"):
        await message.answer(t("adm_grw_lot_disabled_hint"))
    # Straight on to the prizes, first place first.
    await state.set_state(AdminStates.lottery_prize)
    await state.update_data(lottery_id=lot.id)
    await message.answer(
        t("adm_grw_lot_prize_ask", rank=t("lot_rank", rank=1)), reply_markup=prizes_done_kb(t, lot.id)
    )


@router.message(AdminStates.lottery_prize, F.text)
async def lottery_prize(
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
    data = await state.get_data()
    lot = await _own_lottery(session, shop.id, int(data.get("lottery_id") or 0))
    if lot is None or lot.status != "open":
        await state.clear()
        await message.answer(t("adm_grw_lot_not_open"))
        return
    prize = parse_lottery_prize(t, message.text or "")
    if prize is None:
        await message.answer(t("adm_grw_lot_prize_bad"), reply_markup=prizes_done_kb(t, lot.id))
        return
    prizes = growth.prize_list(lot)  # a lottery from before ranked prizes keeps its own
    if len(prizes) >= growth.MAX_LOTTERY_PRIZES:
        await message.answer(t("adm_grw_lot_prize_max", n=growth.MAX_LOTTERY_PRIZES))
        return
    before = {"prizes": prizes}
    lot.prizes = [*prizes, prize]
    lot.winners, lot.prize, lot.prize_amount = len(lot.prizes), "", 0
    await _lottery_log(session, shop, staff, "prizes", lot, before)
    await session.commit()
    saved = t(
        "adm_grw_lot_prize_saved",
        rank=t("lot_rank", rank=len(lot.prizes)),
        prize=log_reports.prize_text(t, prize),
    )
    if len(lot.prizes) >= growth.MAX_LOTTERY_PRIZES:
        await state.clear()
        await message.answer(saved)
        await message.answer(await lottery_text(session, t, lot), reply_markup=lottery_kb(t, lot))
        return
    ask = t("adm_grw_lot_prize_ask", rank=t("lot_rank", rank=len(lot.prizes) + 1))
    await message.answer(f"{saved}\n\n{ask}", reply_markup=prizes_done_kb(t, lot.id))
