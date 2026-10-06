from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Lang, Nav
from paurubot.bot.context import StaffIdentity, forget_join_check
from paurubot.bot.utils import Btn
from paurubot.core import customers
from paurubot.db.models import Customer, Shop
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="start")


def menu(t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None):  # type: ignore[no-untyped-def]
    return keyboards.main_menu(
        t,
        is_staff=staff is not None,
        trial=bool(cfg["trial_enabled"]),
        referral=bool(cfg["referral_enabled"]),
    )


async def home(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, key: str = "welcome"
) -> None:
    await message.answer(t(key), reply_markup=menu(t, cfg, staff))


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    shop: Shop,
    customer: Customer,
    is_new_customer: bool,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    state: FSMContext,
) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    if is_new_customer:
        if arg.startswith("ref_") and arg[4:].isdigit() and cfg["referral_enabled"]:
            referrer = await customers.set_referrer(session, customer, int(arg[4:]))
            if referrer is not None:
                await queue.enqueue(
                    session,
                    "notify",
                    "referral_joined",
                    {"customer_id": referrer.id, "name": customer.display_name},
                    shop_id=shop.id,
                )
        await queue.enqueue(
            session,
            "notify",
            "report",
            {
                "topic": "users",
                "key": "report_new_user",
                "params": {"user": customer.display_name, "telegram_id": customer.telegram_id},
            },
            shop_id=shop.id,
        )
    if arg.startswith("pay_paid_"):
        await home(message, t, cfg, staff, "payment_return_paid")
        return
    if arg.startswith("pay_failed_"):
        await home(message, t, cfg, staff, "payment_return_failed")
        return
    await home(message, t, cfg, staff)


@router.message(Command("cancel"))
@router.message(Btn("btn_cancel"))
async def cancel(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, state: FSMContext
) -> None:
    await state.clear()
    await home(message, t, cfg, staff, "canceled")


@router.callback_query(Nav.filter(F.to == "joined"))
async def joined(
    call: CallbackQuery, customer: Customer, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None
) -> None:
    forget_join_check(customer.telegram_id)
    await call.answer(t("join_thanks"))
    if isinstance(call.message, Message):
        await call.message.answer(t("join_ok"), reply_markup=menu(t, cfg, staff))


@router.callback_query(Nav.filter(F.to == "terms"))
async def accept_terms(
    call: CallbackQuery, customer: Customer, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None
) -> None:
    customer.terms_accepted_version = int(cfg["terms_version"])
    await call.answer(t("terms_accepted"))
    if isinstance(call.message, Message):
        await call.message.answer(t("welcome"), reply_markup=menu(t, cfg, staff))


@router.callback_query(Nav.filter(F.to == "noop"))
async def noop(call: CallbackQuery) -> None:
    await call.answer()


@router.message(F.contact)
async def contact(
    message: Message,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    state: FSMContext,
) -> None:
    assert message.contact is not None
    if message.contact.user_id != customer.telegram_id:
        await message.answer(t("phone_not_yours"))
        return
    phone = message.contact.phone_number.lstrip("+")
    allowed = [str(code).lstrip("+") for code in cfg["phone_country_codes"] or []]
    if allowed and not any(phone.startswith(code) for code in allowed):
        await message.answer(t("phone_country_not_allowed"))
        return
    customer.phone = phone
    await state.clear()
    await home(message, t, cfg, staff, "phone_saved")


@router.message(Btn("btn_guide"))
@router.message(Command("help"))
async def guide(message: Message, t: Texts) -> None:
    await message.answer(t("guide_text"), disable_web_page_preview=True)


@router.message(Btn("btn_language"))
@router.message(Command("language"))
async def language(message: Message, t: Texts, cfg: dict[str, Any]) -> None:
    await message.answer(t("choose_language"), reply_markup=keyboards.languages(t, cfg["languages"]))


@router.callback_query(Lang.filter())
async def set_language(
    call: CallbackQuery,
    callback_data: Lang,
    session: AsyncSession,
    shop: Shop,
    customer: Customer,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    if callback_data.code not in (cfg["languages"] or []):
        await call.answer()
        return
    customer.locale = callback_data.code
    new_t = t.with_locale(callback_data.code)
    await call.answer(new_t("language_changed"))
    if isinstance(call.message, Message):
        await call.message.answer(new_t("welcome"), reply_markup=menu(new_t, cfg, staff))
