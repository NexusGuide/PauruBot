"""Discount codes owned by a reseller's sales bot (TEN-19)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Rb
from paurubot.bot.routers.resale_bot import _actor, _ask, _running, _state_target
from paurubot.bot.states import ResaleBotStates
from paurubot.bot.utils import show
from paurubot.core.formatting import esc, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, DiscountCode, DiscountKind
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="resale_discounts")
CODE_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


async def _show_codes(
    call: CallbackQuery,
    session: AsyncSession,
    child: ShopRow,
    t: Texts,
) -> None:
    rows = list(
        await session.scalars(
            select(DiscountCode)
            .where(DiscountCode.shop_id == child.id, DiscountCode.is_active.is_(True))
            .order_by(DiscountCode.id.desc())
            .limit(20)
        )
    )
    lines = [t("rb_discounts_title")]
    kb = InlineKeyboardBuilder()
    for code in rows:
        expiry = t("rb_discount_unlimited")
        if code.expires_at:
            expiry = code.expires_at.strftime("%Y-%m-%d")
        uses = f"{code.used_count}/{code.max_uses}" if code.max_uses else f"{code.used_count}/∞"
        lines.append(f"• <code>{esc(code.code)}</code> · {code.value}% · {uses} · {expiry}")
        kb.button(
            text=t("rb_btn_discount_delete", code=code.code)[:64],
            callback_data=Rb(action="code_del", id=code.id),
        )
    if not rows:
        lines.append(t("rb_discounts_empty"))
    kb.button(text=t("rb_btn_discount_new"), callback_data=Rb(action="code_new"))
    kb.button(text=t("btn_back"), callback_data=Rb(action="home"))
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


@router.callback_query(Rb.filter(F.action.in_({"discounts", "code_new", "code_del"})))
async def discount_codes(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: Any,
    t: Texts,
    state: FSMContext,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    profile, child = found
    if callback_data.action == "code_new":
        await call.answer()
        await state.set_state(ResaleBotStates.discount_code)
        await _ask(call, t("rb_discount_new_ask"), t)
        return
    if callback_data.action == "code_del":
        code = await session.scalar(
            select(DiscountCode).where(
                DiscountCode.id == callback_data.id,
                DiscountCode.shop_id == child.id,
                DiscountCode.is_active.is_(True),
            )
        )
        if code is None:
            await call.answer(t("button_expired"), show_alert=True)
            return
        code.is_active = False
        await record(
            session,
            shop_id=profile.shop_id,
            actor=_actor(customer, staff),
            action="reseller_bot.discount_deactivated",
            entity="discount_code",
            entity_id=code.id,
            after={"code": code.code},
        )
        await session.commit()
        await call.answer(t("rb_discount_deleted"))
    else:
        await call.answer()
    await _show_codes(call, session, child, t)


@router.message(ResaleBotStates.discount_code, F.text)
async def discount_code_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: Any,
    t: Texts,
    state: FSMContext,
) -> None:
    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    profile, child = found
    parts = (message.text or "").strip().upper().replace(",", " ").split()
    if len(parts) not in {2, 3, 4} or not CODE_SHAPE.fullmatch(parts[0]):
        await message.answer(t("rb_discount_invalid"))
        return
    code_text = parts[0]
    percent = parse_int(parts[1])
    max_uses = None if len(parts) < 3 or parts[2] in {"-", "0"} else parse_int(parts[2])
    days = None if len(parts) < 4 or parts[3] in {"-", "0"} else parse_int(parts[3])
    if percent is None or not 1 <= percent <= 100:
        await message.answer(t("rb_discount_invalid"))
        return
    if parts[2:3] and parts[2] not in {"-", "0"} and (max_uses is None or max_uses < 1):
        await message.answer(t("rb_discount_invalid"))
        return
    if parts[3:4] and parts[3] not in {"-", "0"} and (days is None or not 1 <= days <= 3650):
        await message.answer(t("rb_discount_invalid"))
        return
    exists = await session.scalar(
        select(DiscountCode.id).where(
            DiscountCode.shop_id == child.id,
            func.lower(DiscountCode.code) == code_text.lower(),
        )
    )
    if exists is not None:
        await message.answer(t("rb_discount_exists"))
        return
    code = DiscountCode(
        shop_id=child.id,
        code=code_text,
        kind=DiscountKind.PERCENT,
        value=percent,
        max_uses=max_uses,
        per_customer_limit=1,
        starts_at=utcnow(),
        expires_at=utcnow() + timedelta(days=days) if days else None,
        note="reseller-created discount",
    )
    session.add(code)
    await session.flush()
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.discount_created",
        entity="discount_code",
        entity_id=code.id,
        after={"code": code.code, "percent": percent, "max_uses": max_uses, "days": days},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("rb_discount_created", code=code.code, percent=percent), reply_markup=None)
