"""Reseller-owned customer broadcasts with a small daily safety cap."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Rb
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.resale_bot import (
    _actor,
    _running,
    _state_target,
    show_home,
)
from paurubot.bot.states import ResaleBotStates
from paurubot.core import resale, resale_broadcasts
from paurubot.core import settings as shop_settings
from paurubot.db.models import Broadcast, Customer, JobStatus
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="resale_broadcast")


@router.callback_query(Rb.filter(F.action == "broadcast"))
async def broadcast_ask(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    _profile, child = found
    if shop.id != child.id:
        await call.answer(t("rb_broadcast_own_bot"), show_alert=True)
        return
    if await resale_broadcasts.remaining_today(session, child.id) <= 0:
        await call.answer(t("rb_broadcast_limit", limit=resale_broadcasts.DAILY_LIMIT), show_alert=True)
        return
    await call.answer()
    await state.set_state(ResaleBotStates.broadcast)
    if isinstance(call.message, Message):
        await call.message.answer(t("rb_broadcast_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResaleBotStates.broadcast)
async def broadcast_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    profile, child = found
    if shop.id != child.id:
        await state.clear()
        await message.answer(t("rb_broadcast_own_bot"))
        return
    if not resale.may_have_bot(profile, await shop_settings.load(session, profile.shop_id)):
        await state.clear()
        await message.answer(t("rb_not_allowed"))
        return
    if not await resale_broadcasts.lock_capacity(session, child.id):
        await state.clear()
        await message.answer(t("rb_broadcast_limit", limit=resale_broadcasts.DAILY_LIMIT))
        return

    row = Broadcast(
        shop_id=child.id,
        created_by_staff_id=staff.id if staff is not None and staff.id else None,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        status=JobStatus.QUEUED,
        audience={"kind": "all", "reseller_bot": True},
    )
    session.add(row)
    await session.flush()
    await queue.enqueue(
        session,
        "broadcast",
        "run",
        {"broadcast_id": row.id},
        shop_id=child.id,
        dedupe_key=f"broadcast:{row.id}",
    )
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.broadcast_queued",
        entity="broadcast",
        entity_id=row.id,
        after={"child_shop_id": child.id, "audience": "own_customers"},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("rb_broadcast_queued"))
    await show_home(message, session, shop, profile, t)
