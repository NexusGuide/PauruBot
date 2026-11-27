from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
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
from paurubot.bot.context import StaffIdentity, forget_join_check, missing_channels
from paurubot.bot.menu import (
    BUTTONS,
    DEFAULT_LAYOUT,
    available,
    inline_menu,
    main_menu,
    menu_mode,
    miniapp_button,
)
from paurubot.bot.utils import Btn, show
from paurubot.core import customers
from paurubot.core.formatting import esc, money
from paurubot.db.models import Customer, Shop
from paurubot.i18n import Texts

router = Router(name="start")


def menu(
    t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, *, trial_used: bool = False
) -> ReplyKeyboardMarkup | InlineKeyboardMarkup | ReplyKeyboardRemove:
    """One mode-aware menu for both entry and completion messages (BOT-12).

    Staff use the selected customer mode too; an inline admin shortcut keeps
    management reachable without forcing the full reply keyboard back on screen.
    """
    mode = menu_mode(cfg)
    if mode == "inline":
        return inline_menu(t, cfg, is_staff=staff is not None, trial_used=trial_used)
    if mode == "miniapp":
        return miniapp_button(t, cfg, is_staff=staff is not None)
    return main_menu(t, cfg, is_staff=staff is not None, trial_used=trial_used)


def bot_off_notice_key(staff: StaffIdentity, cfg: dict[str, Any]) -> str:
    """The "bot is off for users" note for staff. A customer's own bot is run from the bot and its
    Mini App, so its manager is never sent to the web panel."""
    from paurubot.bot.routers.admin_navigation import customer_bot

    if not customer_bot(staff, cfg):
        return "staff_bot_off_notice"
    return "staff_bot_off_notice_own" if cfg.get("own_bot") else "staff_bot_off_notice_resale"


async def home(
    message: Message,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    key: str = "welcome",
    *,
    trial_used: bool = False,
    **params: Any,
) -> None:
    if menu_mode(cfg) != "reply":
        # Telegram cannot remove a reply keyboard and attach inline buttons in
        # the same message. Failure to delete this cosmetic message is harmless.
        try:
            gone = await message.answer("⌨️", reply_markup=ReplyKeyboardRemove())
            await gone.delete()
        except Exception:  # cosmetic only
            pass
    if staff is not None and not cfg.get("bot_enabled", True):
        # Staff still use a switched-off bot (to work during maintenance); say so, or the switch
        # looks as if it did nothing while customers only get the maintenance message.
        await message.answer(t(bot_off_notice_key(staff, cfg)))
    from aiogram.exceptions import TelegramBadRequest

    markup = menu(t, cfg, staff, trial_used=trial_used)
    try:
        sent = await message.answer(t(key, **params), reply_markup=markup)
    except TelegramBadRequest:
        sent = await message.answer(t(key, **params), reply_markup=markup, parse_mode=None)
    # Only the plain menu is replaced. One that carries an outcome ("payment received", "added to
    # your account") stays in the chat, or the next menu would take the customer's record (AUD-7).
    if cfg.get("menu_cleanup") and message.chat.type == "private" and key == "welcome":
        await replace_home(message.chat.id, sent)


HOME_TTL = 48 * 3600  # Telegram lets a bot delete its own messages for 48 hours


async def replace_home(chat_id: int, sent: Message) -> None:
    """Delete the chat's previous home menu now that `sent` is the current one.

    Only the message the bot itself sent as the home menu is ever deleted: its id is kept per
    bot and chat. A message that is already gone or too old to delete is simply left.
    """
    from paurubot import kv

    if sent.bot is None:
        return
    key = f"home:{sent.bot.id}:{chat_id}"
    try:
        previous = (await kv.marked([key])).get(key)
        await kv.mark(key, HOME_TTL, str(sent.message_id))
    except Exception:  # Redis trouble must never cost the customer the menu itself
        return
    if previous and previous.isdigit() and int(previous) != sent.message_id:
        try:
            await sent.bot.delete_message(chat_id, int(previous))
        except Exception:  # deleted by the customer, too old, or not ours to delete
            pass


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
    call: CallbackQuery,
    callback_data: Menu,
    t: Texts,
    cfg: dict[str, Any],
    dispatcher: Any,
    bot: Bot,
    state: FSMContext,
    staff: StaffIdentity | None = None,
    **data: Any,
) -> None:
    """Route an inline menu press inside the *current* update context.

    Re-entering feed_update opens a second database session: the customer upsert
    waits on the row locked by this callback, which cannot finish until that
    nested update returns. Reuse the session, shop, customer and FSM instead.
    """
    await call.answer()
    if not isinstance(call.message, Message) or call.message.chat.type != "private":
        return
    if call.message.chat.id != call.from_user.id:
        return
    if menu_mode(cfg) == "miniapp":
        # An old glass menu must not reopen the bot menu after changing modes.
        await state.clear()
        await home(call.message, t, cfg, staff)
        return
    await open_item(
        callback_data.item, call.message, call.from_user, t, cfg, staff, dispatcher, bot, state, data
    )


def item_shown(item: str, cfg: dict[str, Any]) -> bool:
    """A main-menu item this customer can open now: on, and placed in the menu."""
    layout = cfg.get("menu_layout") or DEFAULT_LAYOUT
    return item in BUTTONS and item in available(cfg) and any(item in row for row in layout)


async def open_item(
    item: str,
    message: Message,
    user: Any,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    dispatcher: Any,
    bot: Bot,
    state: FSMContext,
    data: dict[str, Any],
) -> None:
    """Open a main-menu item as if its button was pressed, inside the current update context."""
    from datetime import UTC, datetime

    from aiogram.types import Chat, Update

    if not item_shown(item, cfg):
        return
    synthetic = Message(
        message_id=message.message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=t(BUTTONS[item]),
    ).as_(bot)
    # Main-menu navigation must not become a ticket body, receipt or other FSM input.
    await state.clear()
    for key in ("handler", "event_router", "callback_data", "command"):
        data.pop(key, None)
    data.update(
        dispatcher=dispatcher,
        bot=bot,
        state=state,
        raw_state=None,
        t=t,
        cfg=cfg,
        staff=staff,
        event_from_user=user,
        event_chat=synthetic.chat,
        event_update=Update(update_id=0, message=synthetic),
    )
    await dispatcher.propagate_event(update_type="message", event=synthetic, **data)


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
    dispatcher: Any,
    bot: Bot,
    **data: Any,
) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    from paurubot.core import referral

    pending, saved_ref = await referral.take_pending_start(shop.id, customer.id)
    welcome_gift = 0
    if is_new_customer or pending:
        ref_id = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else saved_ref
        welcome_gift = await referral.complete_onboarding(
            session, shop.id, customer, cfg, ref_telegram_id=ref_id
        )
    if arg.startswith("cust_") and arg[5:].isdigit():
        # «Manage user» under a report in the shop's group: the customer's card, for staff only.
        if staff is not None and staff.can("customers.read"):
            from paurubot.bot.routers.admin import customer_card

            found = await customers.get(session, shop.id, int(arg[5:]))
            if found is not None:
                text, kb = await customer_card(session, t, found, staff)
                await message.answer(text, reply_markup=kb)
                return
        await home(message, t, cfg, staff)
        return
    if arg.startswith("claim_"):
        from paurubot.core import guest

        try:
            moved = await guest.claim(session, arg[6:], customer)
        except guest.GuestError:
            await home(message, t, cfg, staff, "guest_claim_invalid")
            return
        await session.commit()
        await home(message, t, cfg, staff, "guest_claimed", count=moved)
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
    await home(message, t, cfg, staff, trial_used=bool(data.get("trial_used")))
    if welcome_gift:
        await message.answer(t("start_gift", amount=money(welcome_gift)))
    if arg.startswith("go_") and message.from_user is not None and menu_mode(cfg) != "miniapp":
        # A quick link from the panel (t.me/<bot>?start=go_<item>): straight into that section.
        await open_item(
            arg[3:],
            message,
            message.from_user,
            t,
            cfg,
            staff,
            dispatcher,
            bot,
            state,
            {
                **data,
                "session": session,
                "shop": shop,
                "customer": customer,
                "is_new_customer": is_new_customer,
            },
        )


@router.message(Command("cancel"))
@router.message(Btn("btn_cancel"))
async def cancel(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, state: FSMContext
) -> None:
    await state.clear()
    await home(message, t, cfg, staff, "canceled")


@router.callback_query(Nav.filter(F.to == "joined"))
async def joined(
    call: CallbackQuery,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    bot: Bot,
    session: AsyncSession,
) -> None:
    # The button alone is not proof: without a fresh check the bot used to confirm users who had
    # never joined (GRW-07). Check again and answer honestly.
    forget_join_check(customer.telegram_id)
    missing = await missing_channels(bot, session, customer)
    if missing:
        await call.answer(t("join_required_alert"), show_alert=True)
        if isinstance(call.message, Message):
            await call.message.answer(t("join_required"), reply_markup=keyboards.join(t, missing))
        return
    from paurubot.core import referral

    pending, saved_ref = await referral.take_pending_start(customer.shop_id, customer.id)
    welcome_gift = 0
    if pending:
        welcome_gift = await referral.complete_onboarding(
            session, customer.shop_id, customer, cfg, ref_telegram_id=saved_ref
        )
    await call.answer(t("join_thanks"))
    if isinstance(call.message, Message):
        await home(call.message, t, cfg, staff, "join_ok")
        if welcome_gift:
            await call.message.answer(t("start_gift", amount=money(welcome_gift)))


@router.callback_query(Nav.filter(F.to == "terms"))
async def accept_terms(
    call: CallbackQuery,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    session: AsyncSession,
) -> None:
    from paurubot.core import referral

    customer.terms_accepted_version = int(cfg["terms_version"])
    pending, saved_ref = await referral.take_pending_start(customer.shop_id, customer.id)
    welcome_gift = 0
    if pending:
        welcome_gift = await referral.complete_onboarding(
            session, customer.shop_id, customer, cfg, ref_telegram_id=saved_ref
        )
    await call.answer(t("terms_accepted"))
    if isinstance(call.message, Message):
        await home(call.message, t, cfg, staff)
        if welcome_gift:
            await call.message.answer(t("start_gift", amount=money(welcome_gift)))


@router.callback_query(Nav.filter(F.to == "rterms"))
async def accept_reseller_terms(
    call: CallbackQuery, customer: Customer, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None
) -> None:
    """A reseller accepts the shop's separate purchase terms; buying goes on from the menu."""
    customer.reseller_terms_version = int(cfg.get("reseller_terms_version") or 1)
    await call.answer(t("terms_accepted"))
    if isinstance(call.message, Message):
        await home(call.message, t, cfg, staff)


@router.callback_query(Nav.filter(F.to == "home"))
async def open_home(
    call: CallbackQuery, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, state: FSMContext
) -> None:
    """A broadcast's "main menu" button (Foxima parity): the home menu, as /start shows it."""
    await call.answer()
    if isinstance(call.message, Message) and call.message.chat.type == "private":
        await state.clear()
        await home(call.message, t, cfg, staff)


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
    from paurubot.core import phones

    phone = phones.allowed(message.contact.phone_number, cfg["phone_country_codes"])
    if phone is None:
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
    await message.answer(t("miniapp_open"), reply_markup=miniapp_button(t, cfg))


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
    from aiogram.exceptions import TelegramBadRequest

    if entry and entry.get("file_id"):
        try:
            await call.message.answer_document(entry["file_id"], caption=caption, reply_markup=kb.as_markup())
            return
        except TelegramBadRequest:
            pass
    await call.message.answer(caption, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.message(Btn("btn_language"))
@router.message(Command("language"))
async def language(message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None) -> None:
    from paurubot.bot.menu import available

    if "language" not in available(cfg):
        await home(message, t, cfg, staff, "unknown_command")
        return
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
    if callback_data.code not in (cfg["languages"] or []) or cfg.get("own_bot"):
        await call.answer()
        return
    customer.locale = callback_data.code
    new_t = t.with_locale(callback_data.code)
    await call.answer(new_t("language_changed"))
    if isinstance(call.message, Message):
        await home(call.message, new_t, cfg, staff)
