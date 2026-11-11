"""Lucky wheel and lotteries in the bot (GRW-03, GRW-04)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.utils import Btn, show
from paurubot.core import growth
from paurubot.core.formatting import esc, jdate, money
from paurubot.db.models import Customer, LotteryEntry
from paurubot.i18n import Texts
from paurubot.notifications import log_reports

router = Router(name="growth")
DICE_ANIMATION = 3.5  # seconds Telegram's dice and slot machine take to settle


class Fun(CallbackData, prefix="g"):
    action: str
    id: int = 0


def _prize_text(t: Texts, prize: dict[str, Any]) -> str:
    if prize["kind"] == "wallet":
        return t("wheel_prize_wallet", amount=money(int(prize["value"])))
    if prize["kind"] == "discount":
        return t("wheel_prize_discount", percent=int(prize["value"]))
    return t("wheel_prize_nothing")


def _spin_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_spin"), callback_data=Fun(action="spin"))
    return kb.as_markup()


@router.message(Btn("btn_wheel"))
async def wheel(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    if not cfg.get("wheel_enabled"):
        await message.answer(t("wheel_off"))
        return
    style = str(cfg.get("wheel_style") or "wheel")
    if style in growth.DICE_EMOJI:
        # Foxima parity: Telegram's dice or slot machine, for one set prize.
        lines = [t(f"luck_{style}_intro", amount=money(int(cfg.get("wheel_dice_prize") or 0)))]
    else:
        try:
            prizes = growth.validate_prizes(cfg.get("wheel_prizes") or [])
        except growth.GrowthError:
            await message.answer(t("wheel_off"))
            return
        lines = [t("wheel_intro")] + [f"• {esc(p['label'])}" for p in prizes]
    when = await growth.next_spin_at(session, customer, cfg)
    from paurubot.db.base import utcnow

    if when is not None and when > utcnow():
        lines.append(t("wheel_wait", date=jdate(when, with_time=True)))
        await message.answer("\n".join(lines))
        return
    await message.answer("\n".join(lines), reply_markup=_spin_kb(t))


@router.callback_query(Fun.filter(F.action == "spin"))
async def do_spin(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, cfg: dict[str, Any]
) -> None:
    style = str(cfg.get("wheel_style") or "wheel")
    try:
        if style in growth.DICE_EMOJI and isinstance(call.message, Message):
            chat = call.message

            async def roll() -> int:
                thrown = await chat.answer_dice(emoji=growth.DICE_EMOJI[style])
                return int(thrown.dice.value) if thrown.dice else 0

            result = await growth.dice_spin(session, customer, cfg, roll=roll)
        else:
            result = await growth.spin(session, customer, cfg)
    except growth.GrowthError as exc:
        await session.rollback()
        if exc.key == "wheel_wait":
            await call.answer(t("wheel_wait", date=jdate(exc.params["at"], with_time=True)), show_alert=True)
        else:
            await call.answer(t(exc.key), show_alert=True)
        return
    await session.commit()
    await call.answer()
    if style in growth.DICE_EMOJI:
        import asyncio

        await asyncio.sleep(DICE_ANIMATION)  # the result after Telegram's animation stops
    text = t("wheel_result", label=esc(result.prize["label"])) + "\n" + _prize_text(t, result.prize)
    if result.code:
        text += "\n" + t("wheel_code", code=esc(result.code))
    await show(call, text)


@router.message(Btn("btn_lottery"))
async def lottery(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    item = await growth.open_lottery(session, customer.shop_id) if cfg.get("lottery_enabled") else None
    if item is None:
        await message.answer(t("lottery_none"))
        return
    prizes = growth.prize_list(item)
    lines = [
        t("lottery_header", title=esc(item.title)),
        t("lottery_ends", date=jdate(item.ends_at, with_time=True), winners=len(prizes)),
    ]
    if prizes:
        lines.append(t("lottery_prizes_title"))
        for rank, prize in enumerate(prizes, start=1):
            lines.append(
                t(
                    "lottery_prize_row",
                    mark=log_reports.rank_mark(rank),
                    rank=t("lot_rank", rank=rank),
                    prize=log_reports.prize_text(t, prize),
                )
            )
    board = await growth.standings(session, item)
    mine = next((row for row in board if row.customer_id == customer.id), None)
    if item.mode != "join":
        lines.append(t(f"lottery_pick_{item.pick}"))
    if item.mode == "points":
        lines.append(t("lottery_by_points", per=money(item.points_per), points=mine.points if mine else 0))
    elif item.mode == "purchases":
        lines.append(t("lottery_by_purchase", tickets=mine.points if mine else 0))
    if item.board in {"self", "top"} and item.mode != "join":
        if mine is None:
            lines.append(t("lottery_no_rank"))
        else:
            lines.append(t("lottery_my_rank", rank=board.index(mine) + 1, total=len(board)))
    if item.board == "top" and item.mode != "join" and board:
        lines.append(t("lottery_top_title"))
        for rank, row in enumerate(board[: growth.BOARD_TOP], start=1):
            other = await session.get(Customer, row.customer_id)
            name = esc(growth.masked(log_reports.person_name(other).lstrip("@") if other else None))
            lines.append(t("lottery_top_row", mark=log_reports.rank_mark(rank), name=name, points=row.points))
    if item.mode != "join":
        await message.answer("\n".join(lines))
        return
    joined = await session.scalar(
        select(LotteryEntry.id).where(
            LotteryEntry.lottery_id == item.id, LotteryEntry.customer_id == customer.id
        )
    )
    if joined:
        lines.append(t("lottery_joined"))
        await message.answer("\n".join(lines))
        return
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_lottery_join"), callback_data=Fun(action="join", id=item.id))
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(Fun.filter(F.action == "join"))
async def join(
    call: CallbackQuery,
    callback_data: Fun,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    from paurubot.db.models import Lottery

    item = await session.get(Lottery, callback_data.id)
    if item is None or item.shop_id != customer.shop_id or not cfg.get("lottery_enabled"):
        await call.answer(t("lottery_none"), show_alert=True)
        return
    try:
        await growth.join(session, item, customer)
    except growth.GrowthError as exc:
        await call.answer(t(exc.key), show_alert=True)
        return
    await session.commit()
    await call.answer(t("lottery_joined"), show_alert=True)
    await show(call, t("lottery_joined"))
