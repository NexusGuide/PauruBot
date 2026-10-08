from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Lang, Menu, Nav
from paurubot.bot.context import StaffIdentity, forget_join_check
from paurubot.bot.menu import BUTTONS, available, inline_menu, main_menu, menu_mode, miniapp_button
from paurubot.bot.utils import Btn, show
from paurubot.core import customers
from paurubot.core.formatting import esc, money
from paurubot.db.models import Customer, Shop
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="start")


def menu(
    t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """The reply keyboard; customers get none when the shop shows its menu inline (BOT-12).
    Staff always keep it, for the admin button."""
    if staff is None and menu_mode(cfg) != "reply":
        return ReplyKeyboardRemove()
    return main_menu(t, cfg, is_staff=staff is not None)


async def home(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, key: str = "welcome"
) -> None:
    mode = menu_mode(cfg) if staff is None else "reply"
    if mode == "reply":
        await message.answer(t(key), reply_markup=menu(t, cfg, staff))
        return
    # Take an old reply keyboard away, then show the menu under the message itself.
    try:
        gone = await message.answer("⌨️", reply_markup=ReplyKeyboardRemove())
        await gone.delete()
    except Exception:  # cosmetic only
        pass
    markup = inline_menu(t, cfg) if mode == "inline" else miniapp_button(t)
    await message.answer(t(key), reply_markup=markup)


async def send_start_media(message: Message, cfg: dict[str, Any]) -> None:
    """A sticker, GIF or picture before the welcome text (BOT-11)."""
    media = cfg.get("start_media") or {}
    file_id = str(media.get("file_id") or "")
    if not file_id:
        return
    try:
        if media.get("type") == "sticker":
            await message.answer_sticker(file_id)
        elif media.get("type") == "animation":
            await message.answer_animation(file_id)
        elif media.get("type") == "photo":
            await message.answer_photo(file_id)
    except Exception:  # an expired file id must never block /start
        pass


@router.callback_query(Menu.filter())
async def menu_press(
    call: CallbackQuery, callback_data: Menu, t: Texts, cfg: dict[str, Any], dispatcher: Any, bot: Any
) -> None:
    """An inline menu button runs exactly what its reply-keyboard button runs."""
    from datetime import UTC, datetime

    from aiogram.types import Chat, Update

    await call.answer()
    label_key = BUTTONS.get(callback_data.item)
    if label_key is None or callback_data.item not in available(cfg):
        return
    synthetic = Message(
        message_id=call.message.message_id if call.message else 0,
        date=datetime.now(UTC),
        chat=Chat(id=call.from_user.id, type="private"),
        from_user=call.from_user,
        text=t(label_key),
    )
    await dispatcher.feed_update(bot, Update(update_id=0, message=synthetic))


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
    welcome_gift = 0
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
        gift = int(cfg["start_gift_amount"] or 0)
        if gift > 0 and (customer.referrer_id or not cfg["start_gift_referred_only"]):
            from paurubot.ledger import wallet

            # A welcome gift (GRW-09): once per customer, even if /start is pressed again.
            posted = await wallet.reward(
                session, shop.id, customer.id, gift, key=f"start_gift:{customer.id}", memo="welcome gift"
            )
            if posted.created:
                welcome_gift = gift
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
    if arg.startswith("claim_"):
        from paurubot.core import guest

        try:
            moved = await guest.claim(session, arg[6:], customer)
        except guest.GuestError:
            await home(message, t, cfg, staff, "guest_claim_invalid")
            return
        await session.commit()
        await message.answer(t("guest_claimed", count=moved), reply_markup=menu(t, cfg, staff))
        return
    if arg.startswith("pay_paid_"):
        await home(message, t, cfg, staff, "payment_return_paid")
        return
    if arg.startswith("pay_wait_"):
        await home(message, t, cfg, staff, "payment_return_wait")
        return
    if arg.startswith("pay_failed_"):
        await home(message, t, cfg, staff, "payment_return_failed")
        return
    await send_start_media(message, cfg)
    await home(message, t, cfg, staff)
    if welcome_gift:
        await message.answer(t("start_gift", amount=money(welcome_gift)))


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


@router.message(Btn("btn_miniapp"))
async def miniapp(message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None) -> None:
    from paurubot.bot.menu import available, miniapp_button

    if "miniapp" not in available(cfg):
        await home(message, t, cfg, staff, "unknown_command")
        return
    await message.answer(t("miniapp_open"), reply_markup=miniapp_button(t))


@router.message(Btn("btn_guide"))
@router.message(Command("help"))
async def guide(message: Message, t: Texts, cfg: dict[str, Any]) -> None:
    items = cfg.get("help_items") or []
    await message.answer(t("guide_text"), reply_markup=help_kb(t, items), disable_web_page_preview=True)


def help_kb(t: Texts, items: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    kb = InlineKeyboardBuilder()
    for index, item in enumerate(items):
        if item.get("text"):
            kb.button(text=item["title"], callback_data=Nav(to="help", arg=index))
        else:
            kb.button(text=item["title"], url=item["url"])
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(Nav.filter(F.to == "help"))
async def help_topic(call: CallbackQuery, callback_data: Nav, t: Texts, cfg: dict[str, Any]) -> None:
    items = cfg.get("help_items") or []
    if not 0 <= callback_data.arg < len(items):
        await call.answer()
        return
    item = items[callback_data.arg]
    kb = InlineKeyboardBuilder()
    if item.get("url"):
        kb.row(InlineKeyboardButton(text=t("btn_open_link"), url=item["url"]))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Nav(to="help_home").pack()))
    await call.answer()
    await show(call, f"<b>{esc(item['title'])}</b>\n\n{esc(item['text'])}", kb.as_markup())


@router.callback_query(Nav.filter(F.to == "help_home"))
async def help_home(call: CallbackQuery, t: Texts, cfg: dict[str, Any]) -> None:
    await call.answer()
    await show(call, t("guide_text"), help_kb(t, cfg.get("help_items") or []))


@router.message(Btn("btn_apps"))
@router.message(Command("apps"))
async def app_list(message: Message, shop: Shop, t: Texts, cfg: dict[str, Any]) -> None:
    from paurubot.core import apps

    items = cfg.get("download_apps") or []
    if not items:
        await message.answer(t("apps_none"))
        return
    kb = InlineKeyboardBuilder()
    for index, app in enumerate(items):
        entry = await apps.cached(shop.id, app) if app.get("repo") else None
        label = t("app_button", name=app["name"], platform=t(f"platform_{app['platform']}"))
        if entry and entry.get("version"):
            label = f"{label} · {entry['version']}"
        kb.button(text=label, callback_data=Nav(to="app", arg=index))
    kb.adjust(1)
    await message.answer(t("apps_intro"), reply_markup=kb.as_markup())


@router.callback_query(Nav.filter(F.to == "app"))
async def app_download(
    call: CallbackQuery, callback_data: Nav, shop: Shop, t: Texts, cfg: dict[str, Any]
) -> None:
    from paurubot.core import apps

    items = cfg.get("download_apps") or []
    if not 0 <= callback_data.arg < len(items) or not isinstance(call.message, Message):
        await call.answer()
        return
    app = items[callback_data.arg]
    entry = await apps.cached(shop.id, app) if app.get("repo") else None
    await call.answer()
    kb = InlineKeyboardBuilder()
    if entry and entry.get("url"):
        kb.row(InlineKeyboardButton(text=t("btn_download_github"), url=entry["url"]))
    if app.get("store_url"):
        kb.row(InlineKeyboardButton(text=t("btn_open_store"), url=app["store_url"]))
    caption = t("app_caption", name=esc(app["name"]), version=esc((entry or {}).get("version") or "-"))
    if entry and entry.get("file_id"):
        await call.message.answer_document(entry["file_id"], caption=caption, reply_markup=kb.as_markup())
    else:
        await call.message.answer(caption, reply_markup=kb.as_markup(), disable_web_page_preview=True)


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
