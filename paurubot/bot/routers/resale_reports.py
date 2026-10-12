"""Telegram UI for reseller profit reports and XLSX export."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.callbacks import Rb
from paurubot.bot.routers.resale_bot import _running
from paurubot.bot.utils import show
from paurubot.core import resale_reports
from paurubot.core.formatting import money
from paurubot.db.models import Customer
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="resale_reports")


async def _render(call: CallbackQuery, session: AsyncSession, child: ShopRow, t: Texts, key: str) -> None:
    report = await resale_reports.summary(session, child.id, key)
    text = t(
        "rb_profit_report",
        period=t(f"rb_profit_{key}"),
        sales=report.sales,
        revenue=money(report.revenue),
        wholesale=money(report.wholesale),
        profit=money(report.profit),
    )
    kb = InlineKeyboardBuilder()
    for item in ("day", "week", "month"):
        kb.button(text=t(f"rb_profit_{item}"), callback_data=Rb(action=f"profit_{item}"))
    for days, label in ((1, "rb_profit_day"), (7, "rb_profit_week"), (30, "rb_profit_month")):
        kb.button(text=t("rb_profit_export", period=t(label)), callback_data=Rb(action="profit_xlsx", page=days))
    kb.button(text=t("btn_back"), callback_data=Rb(action="home"))
    kb.adjust(1)
    await show(call, text, kb.as_markup())


@router.callback_query(Rb.filter(F.action.in_({"profit", "profit_day", "profit_week", "profit_month", "profit_xlsx"})))
async def profit_report(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: Any,
    t: Texts,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    _profile, child = found
    if callback_data.action == "profit_xlsx":
        days = callback_data.page if callback_data.page in {1, 7, 30} else 30
        key = {1: "day", 7: "week", 30: "month"}[days]
        data = await resale_reports.workbook(session, child.id, key)
        if isinstance(call.message, Message):
            await call.message.answer_document(
                BufferedInputFile(data, filename=f"reseller-profit-{key}.xlsx"),
                caption=t("rb_profit_file_caption", period=t(f"rb_profit_{key}")),
            )
        await call.answer(t("rb_profit_exported"))
        await _render(call, session, child, t, key)
        return
    key = "month" if callback_data.action == "profit" else callback_data.action.removeprefix("profit_")
    await call.answer()
    await _render(call, session, child, t, key)
