"""Self-service branding and access-gate settings for reseller bots."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Rb
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.resale_bot import _actor, _running, _state_target, show_home
from paurubot.bot.states import ResaleCustomizeStates
from paurubot.core import resale_customization
from paurubot.core import settings as shop_settings
from paurubot.db.models import BotText, Customer, ForcedChannel
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.i18n import invalidate as invalidate_texts

router = Router(name="resale_customization")


def customize_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("rb_customize_welcome"), callback_data=Rb(action="customize_welcome"))
    kb.button(text=t("rb_customize_rules"), callback_data=Rb(action="customize_rules"))
    kb.button(text=t("rb_customize_guide"), callback_data=Rb(action="customize_guide"))
    kb.button(text=t("rb_customize_logo"), callback_data=Rb(action="customize_logo"))
    kb.button(text=t("rb_customize_channel"), callback_data=Rb(action="customize_channel"))
    kb.button(text=t("btn_back"), callback_data=Rb(action="home"))
    kb.adjust(1)
    return kb.as_markup()


async def _show_customization(call: CallbackQuery, t: Texts) -> None:
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(t("rb_customize_title"), reply_markup=customize_kb(t))


@router.callback_query(Rb.filter(F.action == "customize"))
async def customize(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    if await _running(call, session, shop, customer, staff, t) is None:
        return
    await _show_customization(call, t)


@router.callback_query(Rb.filter(F.action.in_(set(resale_customization.TEXT_KEYS))))
async def text_ask(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    if await _running(call, session, shop, customer, staff, t) is None:
        return
    await call.answer()
    await state.set_state(ResaleCustomizeStates.text)
    await state.update_data(customize_key=resale_customization.TEXT_KEYS[callback_data.action])
    if isinstance(call.message, Message):
        await call.message.answer(t("rb_customize_text_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResaleCustomizeStates.text, F.text)
async def text_got(
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
    data = await state.get_data()
    key = str(data.get("customize_key") or "")
    if key not in resale_customization.TEXT_KEYS.values():
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value or len(value) > resale_customization.MAX_CUSTOM_TEXT:
        await message.answer(t("rb_customize_text_invalid", max=resale_customization.MAX_CUSTOM_TEXT))
        return
    row = await session.scalar(
        select(BotText).where(BotText.shop_id == child.id, BotText.locale == t.locale, BotText.key == key)
    )
    if row is None:
        session.add(BotText(shop_id=child.id, locale=t.locale, key=key, value=value))
    else:
        row.value = value
    values = {"terms_required": True} if key == "terms_text" else {}
    if values:
        await shop_settings.set_many(session, child.id, values)
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.customized_text",
        entity="shop",
        entity_id=child.id,
        after={"locale": t.locale, "key": key},
    )
    await session.commit()
    invalidate_texts(child.id)
    await state.clear()
    await message.answer(t("rb_customize_saved"))
    await show_home(message, session, shop, profile, t)


@router.callback_query(Rb.filter(F.action == "customize_logo"))
async def logo_ask(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    if await _running(call, session, shop, customer, staff, t) is None:
        return
    await call.answer()
    await state.set_state(ResaleCustomizeStates.logo)
    if isinstance(call.message, Message):
        await call.message.answer(t("rb_customize_logo_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResaleCustomizeStates.logo)
async def logo_got(
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
    if message.text and message.text.strip() == "-":
        media = {}
    elif message.photo:
        media = {"type": "photo", "file_id": message.photo[-1].file_id}
    else:
        await message.answer(t("rb_customize_logo_invalid"))
        return
    await shop_settings.set_many(session, child.id, {"start_media": media})
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.customized_logo",
        entity="shop",
        entity_id=child.id,
        after={"set": bool(media)},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("rb_customize_saved"))
    await show_home(message, session, shop, profile, t)


@router.callback_query(Rb.filter(F.action == "customize_channel"))
async def channel_ask(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    if await _running(call, session, shop, customer, staff, t) is None:
        return
    await call.answer()
    await state.set_state(ResaleCustomizeStates.channel)
    if isinstance(call.message, Message):
        await call.message.answer(t("rb_customize_channel_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResaleCustomizeStates.channel, F.text)
async def channel_got(
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
    raw = (message.text or "").strip()
    if raw == "-":
        await session.execute(delete(ForcedChannel).where(ForcedChannel.shop_id == child.id))
        enabled = False
    else:
        parsed = resale_customization.parse_channel(raw)
        if parsed is None:
            await message.answer(t("rb_customize_channel_invalid"))
            return
        chat_id, url = parsed
        await session.execute(delete(ForcedChannel).where(ForcedChannel.shop_id == child.id))
        session.add(ForcedChannel(shop_id=child.id, chat_id=chat_id, title=chat_id, url=url))
        enabled = True
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.customized_channel",
        entity="shop",
        entity_id=child.id,
        after={"enabled": enabled},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("rb_customize_saved"))
    await show_home(message, session, shop, profile, t)
