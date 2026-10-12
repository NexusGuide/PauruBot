"""A reseller's own sales bot, built and run from Telegram (TEN-17).

The reseller opens "my sales bot" from its panel in the main bot (or, as the merchant, from the
admin menu of the bot itself), sends the token made with @BotFather and the bot starts selling at
once. From the same screen it sets its markup, fixed plan prices, cards, support username, turns
the bot on or off, swaps the token or removes the bot. The shop's staff can do the same from a
reseller's card: build the bot with the reseller's token, stop it, or let the reseller build one.
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin, Rb, Rs
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.start import menu
from paurubot.bot.states import AdminStates, ResaleBotStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import catalog, resale
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money, parse_int
from paurubot.db.models import Customer, ResellerProfile, StaffRole
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="resale_bot")
PLANS_PAGE = 10
SUPPORT_SHAPE = r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$"


# --- whose bot ------------------------------------------------------------------------------


async def _profile(
    session: AsyncSession, shop: ShopRow, customer: Customer, staff: StaffIdentity | None
) -> ResellerProfile | None:
    """In a reseller's own bot only its merchant manages it; in the main bot, the reseller."""
    if shop.reseller_profile_id is not None:
        if staff is None or staff.role != StaffRole.MERCHANT:
            return None
        return await session.get(ResellerProfile, shop.reseller_profile_id)
    profile = customer.reseller_profile
    return profile if profile is not None and profile.is_current else None


async def _allowed(session: AsyncSession, shop: ShopRow, profile: ResellerProfile) -> bool:
    """The same right in both bots: taken away by staff, the reseller can't manage it anywhere."""
    return resale.may_have_bot(profile, await shop_settings.load(session, profile.shop_id))


def _back(shop: ShopRow) -> str:
    return (Admin(section="home") if shop.reseller_profile_id is not None else Rs(action="home")).pack()


def _actor(customer: Customer, staff: StaffIdentity | None) -> Actor:
    return Actor("staff", staff.id) if staff is not None and staff.id else Actor("customer", customer.id)


async def _ask(event: CallbackQuery, text: str, t: Texts) -> None:
    if isinstance(event.message, Message):
        await event.message.answer(text, reply_markup=keyboards.cancel(t))


async def _forget_token_message(message: Message) -> None:
    try:
        await message.delete()  # a bot token is a password; keep it out of the chat
    except Exception:
        pass


# --- the reseller's screen ------------------------------------------------------------------


async def status_text(session: AsyncSession, t: Texts, profile: ResellerProfile) -> tuple[str, str]:
    """The bot's state for its reseller or for staff: running, blocked, metered or none."""
    shop = await resale.shop_of(session, profile)
    if shop is None or shop.bot_token_enc is None:
        blocked = bool(
            shop is not None
            and (await shop_settings.load(session, shop.id, fresh=True)).get("resale_blocked")
        )
        state = "blocked" if blocked else ("metered" if profile.is_metered else "none")
        return t({"blocked": "rb_blocked", "metered": "rb_needs_prepaid", "none": "rb_intro"}[state]), state
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    numbers = await resale.figures(session, shop)
    cards = [c for c in cfg.get("card_accounts") or [] if isinstance(c, dict)]
    text = t(
        "rb_status",
        bot=esc(shop.bot_username or "-"),
        title=esc(shop.title),
        state=t("rb_on") if cfg.get("bot_enabled", True) else t("rb_off"),
        markup=int(cfg.get("resale_markup_percent") or 0),
        prices=len(cfg.get("resale_prices") or {}),
        cards=len(cards),
        support=esc(cfg.get("support_username") or "-"),
        customers=numbers.customers,
        active=numbers.active,
        orders=numbers.orders_month,
        revenue=money(numbers.revenue_month),
    )
    if not cards:
        text += "\n\n" + t("rb_no_cards_warning")
    return text, "running"


def home_kb(
    t: Texts, running: bool, can_connect: bool, back: str, username: str | None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if running:
        kb.button(text=t("rb_btn_markup"), callback_data=Rb(action="markup"))
        kb.button(text=t("rb_btn_prices"), callback_data=Rb(action="prices"))
        kb.button(text=t("rb_btn_cards"), callback_data=Rb(action="cards"))
        kb.button(text=t("rb_btn_support"), callback_data=Rb(action="support"))
        kb.button(text=t("rb_btn_customize"), callback_data=Rb(action="customize"))
        kb.button(text=t("rb_btn_toggle"), callback_data=Rb(action="toggle"))
        kb.button(text=t("rb_btn_token_swap"), callback_data=Rb(action="token"))
        kb.button(text=t("rb_btn_delete"), callback_data=Rb(action="delete"))
    elif can_connect:
        kb.button(text=t("rb_btn_connect"), callback_data=Rb(action="token"))
    kb.adjust(2)
    if running and username:
        kb.row(InlineKeyboardButton(text=t("rb_btn_open", bot=username), url=f"https://t.me/{username}"))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back))
    return kb.as_markup()


async def show_home(
    event: Message | CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    profile: ResellerProfile,
    t: Texts,
) -> None:
    text, status = await status_text(session, t, profile)
    child = await resale.shop_of(session, profile)
    running = status == "running"
    await show(
        event,
        text,
        home_kb(t, running, status == "none", _back(shop), child.bot_username if child else None),
    )


async def _entry(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> ResellerProfile | None:
    profile = await _profile(session, shop, customer, staff)
    if profile is None or not await _allowed(session, shop, profile):
        await call.answer(t("rb_not_allowed"), show_alert=True)
        return None
    return profile


@router.callback_query(Rb.filter(F.action == "home"))
async def home(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    await state.clear()
    profile = await _entry(call, session, shop, customer, staff, t)
    if profile is None:
        return
    await call.answer()
    await show_home(call, session, shop, profile, t)


@router.callback_query(Rb.filter(F.action == "token"))
async def token_ask(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    state: FSMContext,
) -> None:
    profile = await _entry(call, session, shop, customer, staff, t)
    if profile is None:
        return
    if profile.is_metered:
        await call.answer(t("rb_needs_prepaid"), show_alert=True)
        return
    await call.answer()
    await state.set_state(ResaleBotStates.token)
    await _ask(call, t("rb_ask_token"), t)


@router.message(ResaleBotStates.token, F.text)
async def token_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    token = (message.text or "").strip()
    await _forget_token_message(message)
    profile = await _profile(session, shop, customer, staff)
    if profile is None or not await _allowed(session, shop, profile):
        await state.clear()
        await message.answer(t("rb_not_allowed"), reply_markup=menu(t, cfg, staff))
        return
    if profile.is_metered:
        await state.clear()
        await message.answer(t("rb_needs_prepaid"), reply_markup=menu(t, cfg, staff))
        return
    try:
        child = await resale.connect(session, profile, token, "")
    except resale.ResaleError as exc:
        await session.rollback()
        await message.answer(t(f"rb_err_{exc.key}"))  # stays in the state: send another token
        return
    resale.set_bot_right(profile, True)  # an open shop's reseller keeps its right on the web
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.connected",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"bot": child.bot_username, "via": "bot"},
    )
    await session.commit()
    await state.clear()
    await _point(child.id)
    await message.answer(
        t("rb_connected", bot=esc(child.bot_username or "")), reply_markup=menu(t, cfg, staff)
    )
    await show_home(message, session, shop, profile, t)


async def _point(shop_id: int) -> None:
    try:
        await resale.point_webhook(shop_id)
    except Exception:  # Telegram can be retried; polling picks the bot up on its own
        pass


async def _running(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> tuple[ResellerProfile, ShopRow] | None:
    profile = await _entry(call, session, shop, customer, staff, t)
    if profile is None:
        return None
    child = await resale.shop_of(session, profile)
    if child is None or child.bot_token_enc is None:
        await call.answer(t("rb_not_connected"), show_alert=True)
        return None
    return profile, child


async def _save(
    session: AsyncSession,
    child: ShopRow,
    profile: ResellerProfile,
    customer: Customer,
    staff: StaffIdentity | None,
    values: dict[str, Any],
) -> None:
    await resale.update(session, child, values)
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.updated",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"settings": sorted(values), "via": "bot"},
    )
    await session.commit()
    shop_settings.invalidate(child.id)


@router.callback_query(Rb.filter(F.action == "toggle"))
async def toggle(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    profile, child = found
    cfg = await shop_settings.load(session, child.id, fresh=True)
    on = not cfg.get("bot_enabled", True)
    await _save(session, child, profile, customer, staff, {"bot_enabled": on})
    await call.answer(t("rb_on") if on else t("rb_off"))
    await show_home(call, session, shop, profile, t)


@router.callback_query(Rb.filter(F.action.in_({"markup", "support", "card_add"})))
async def value_ask(
    call: CallbackQuery,
    callback_data: Rb,
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
    await call.answer()
    cfg = await shop_settings.load(session, found[1].id, fresh=True)
    if callback_data.action == "markup":
        await state.set_state(ResaleBotStates.markup)
        await _ask(call, t("rb_ask_markup", now=int(cfg.get("resale_markup_percent") or 0)), t)
    elif callback_data.action == "support":
        await state.set_state(ResaleBotStates.support)
        await _ask(call, t("rb_ask_support", now=esc(cfg.get("support_username") or "-")), t)
    else:
        await state.set_state(ResaleBotStates.card)
        await _ask(call, t("rb_ask_card"), t)


async def _state_target(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    state: FSMContext,
) -> tuple[ResellerProfile, ShopRow] | None:
    profile = await _profile(session, shop, customer, staff)
    child = await resale.shop_of(session, profile) if profile is not None else None
    if profile is None or child is None or child.bot_token_enc is None:
        await state.clear()
        return None
    return profile, child


async def _done(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    profile: ResellerProfile,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity | None,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await show_home(message, session, shop, profile, t)


@router.message(ResaleBotStates.markup, F.text)
async def markup_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    value = parse_int(message.text)
    if value is None or not 0 <= value <= resale.MAX_MARKUP:
        await message.answer(t("rb_markup_invalid", max=resale.MAX_MARKUP))
        return
    await _save(session, found[1], found[0], customer, staff, {"resale_markup_percent": value})
    await _done(message, session, shop, found[0], t, cfg, staff, state)


@router.message(ResaleBotStates.support, F.text)
async def support_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    import re

    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    text = (message.text or "").strip()
    if text in {"-", "0"}:
        value = ""
    elif re.match(SUPPORT_SHAPE, text):
        value = "@" + text.lstrip("@")
    else:
        await message.answer(t("rb_support_invalid"))
        return
    await _save(session, found[1], found[0], customer, staff, {"support_username": value})
    await _done(message, session, shop, found[0], t, cfg, staff, state)


# --- cards ----------------------------------------------------------------------------------


def _cards(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(c) for c in cfg.get("card_accounts") or [] if isinstance(c, dict)]


@router.callback_query(Rb.filter(F.action.in_({"cards", "card_del"})))
async def cards(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    from paurubot.core.cards import masked

    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    profile, child = found
    items = _cards(await shop_settings.load(session, child.id, fresh=True))
    if callback_data.action == "card_del":
        if not 0 <= callback_data.id < len(items):
            await call.answer(t("button_expired"), show_alert=True)
            return
        items.pop(callback_data.id)
        await _save(session, child, profile, customer, staff, {"card_accounts": items})
    await call.answer()
    lines = [t("rb_cards_title")]
    kb = InlineKeyboardBuilder()
    for index, item in enumerate(items):
        label = f"{masked(str(item.get('number', '')))} {item.get('holder') or ''}".strip()
        lines.append(
            f"{index + 1}. <code>{esc(str(item.get('number', '')))}</code> {esc(item.get('holder') or '')}"
        )
        kb.button(text=t("rb_btn_card_del", card=label)[:64], callback_data=Rb(action="card_del", id=index))
    if not items:
        lines.append(t("rb_cards_empty"))
    if len(items) < resale.MAX_CARDS:
        kb.button(text=t("rb_btn_card_add"), callback_data=Rb(action="card_add"))
    kb.button(text=t("btn_back"), callback_data=Rb(action="home"))
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


@router.message(ResaleBotStates.card, F.text)
async def card_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    profile, child = found
    try:
        card = resale.parse_card(message.text or "")
    except resale.ResaleError:
        await message.answer(t("rb_card_invalid"))
        return
    items = _cards(await shop_settings.load(session, child.id, fresh=True))
    if any(str(item.get("number")) == card["number"] for item in items):
        await message.answer(t("rb_card_exists"))
        return
    if len(items) >= resale.MAX_CARDS:
        await state.clear()
        await message.answer(t("rb_cards_full"), reply_markup=menu(t, cfg, staff))
        return
    items.append(card)
    await _save(session, child, profile, customer, staff, {"card_accounts": items, "card_enabled": True})
    await _done(message, session, shop, profile, t, cfg, staff, state)


# --- plan prices ----------------------------------------------------------------------------


@router.callback_query(Rb.filter(F.action == "prices"))
async def prices(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    _profile_row, child = found
    plans = await catalog.plans(session, child.id, None)
    await call.answer()
    page = max(0, callback_data.page)
    shown = plans[page * PLANS_PAGE : (page + 1) * PLANS_PAGE]
    sale = await resale.for_shop(session, child.id)
    kb = InlineKeyboardBuilder()
    for plan in shown:
        price = await resale.list_price(session, plan, child.id)
        fixed = "📌 " if sale is not None and str(plan.id) in sale.prices else ""
        kb.row(
            InlineKeyboardButton(
                text=f"{fixed}{plan.title} · {money(price)}"[:64],
                callback_data=Rb(action="price", id=plan.id).pack(),
            )
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text=t("btn_prev"), callback_data=Rb(action="prices", page=page - 1).pack())
        )
    if (page + 1) * PLANS_PAGE < len(plans):
        nav.append(
            InlineKeyboardButton(text=t("btn_next"), callback_data=Rb(action="prices", page=page + 1).pack())
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Rb(action="home").pack()))
    await show(call, t("rb_prices_title") if plans else t("no_plans"), kb.as_markup())


@router.callback_query(Rb.filter(F.action == "price"))
async def price_ask(
    call: CallbackQuery,
    callback_data: Rb,
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
    child = found[1]
    plan = await catalog.get_plan(session, child.id, callback_data.id, None)
    sale = await resale.for_shop(session, child.id)
    if plan is None or sale is None:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    await call.answer()
    cost = await resale.wholesale(session, sale, plan.id, int(plan.price))
    await state.set_state(ResaleBotStates.plan_price)
    await state.update_data(rb_plan=plan.id)
    await _ask(
        call,
        t(
            "rb_ask_price",
            plan=esc(plan.title),
            cost=money(cost),
            now=money(await resale.list_price(session, plan, child.id)),
        ),
        t,
    )


@router.message(ResaleBotStates.plan_price, F.text)
async def price_got(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    found = await _state_target(message, session, shop, customer, staff, state)
    if found is None:
        return
    profile, child = found
    value = parse_int(message.text)
    if value is None or not 0 <= value <= 10**10:
        await message.answer(t("number_expected"))
        return
    plan_id = str((await state.get_data()).get("rb_plan") or "")
    sale = await resale.for_shop(session, child.id)
    if sale is None or not plan_id:
        await state.clear()
        return
    fixed = dict(sale.prices)
    if value == 0:
        fixed.pop(plan_id, None)  # back to the markup
    else:
        fixed[plan_id] = value
    await _save(session, child, profile, customer, staff, {"resale_prices": fixed})
    await _done(message, session, shop, profile, t, cfg, staff, state)


# --- removing the bot -----------------------------------------------------------------------


@router.callback_query(Rb.filter(F.action.in_({"delete", "delete_go"})))
async def delete(
    call: CallbackQuery,
    callback_data: Rb,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    staff: StaffIdentity | None,
    t: Texts,
) -> None:
    found = await _running(call, session, shop, customer, staff, t)
    if found is None:
        return
    profile, child = found
    if callback_data.action == "delete":
        await call.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text=t("rb_btn_delete_yes"), callback_data=Rb(action="delete_go"))
        kb.button(text=t("btn_back"), callback_data=Rb(action="home"))
        kb.adjust(1)
        await show(call, t("rb_delete_confirm", bot=esc(child.bot_username or "")), kb.as_markup())
        return
    in_own_bot = shop.reseller_profile_id is not None
    await resale.disconnect(session, profile)
    await record(
        session,
        shop_id=profile.shop_id,
        actor=_actor(customer, staff),
        action="reseller_bot.stopped",
        entity="reseller_profile",
        entity_id=profile.id,
    )
    await session.commit()
    await call.answer(t("rb_deleted"), show_alert=True)
    if in_own_bot:
        await show(call, t("rb_deleted"))  # this very bot stops answering now
        return
    await show_home(call, session, shop, profile, t)


# --- staff: a reseller's bot from its card --------------------------------------------------


async def _reseller(session: AsyncSession, shop: ShopRow, customer_id: int) -> ResellerProfile | None:
    return await session.scalar(
        select(ResellerProfile).where(
            ResellerProfile.shop_id == shop.id, ResellerProfile.customer_id == customer_id
        )
    )


async def staff_view(
    event: Message | CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    profile: ResellerProfile,
) -> None:
    text, status = await status_text(session, t, profile)
    running = status == "running"
    child = await resale.shop_of(session, profile)
    cfg = await shop_settings.load(session, child.id, fresh=True) if child is not None else {}
    who = await session.get(Customer, profile.customer_id)
    right = resale.BOT_RIGHT in (profile.permissions or [])
    head = t("adm_rbot_title", name=esc(who.display_name if who else "-"))
    kb = InlineKeyboardBuilder()
    if not profile.is_metered:
        kb.button(
            text=t("adm_rbot_btn_token_swap") if running else t("adm_rbot_btn_build"),
            callback_data=Admin(section="rbot", action="token", id=profile.customer_id),
        )
    if running:
        kb.button(
            text=t("adm_rbot_btn_stop"),
            callback_data=Admin(section="rbot", action="stop", id=profile.customer_id),
        )
    if cfg.get("resale_blocked"):
        kb.button(
            text=t("adm_rbot_btn_allow"),
            callback_data=Admin(section="rbot", action="allow", id=profile.customer_id),
        )
    kb.button(
        text=t("adm_rbot_btn_right", state=t("yes") if right else t("no")),
        callback_data=Admin(section="rbot", action="right", id=profile.customer_id),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=profile.customer_id))
    kb.adjust(1)
    await show(event, head + "\n\n" + text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "rbot"), StaffOnly("resellers.manage"))
async def staff_bot(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    profile = await _reseller(session, shop, callback_data.id)
    if profile is None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    action = callback_data.action
    if action == "token":
        if profile.is_metered:
            await call.answer(t("rb_needs_prepaid"), show_alert=True)
            return
        await call.answer()
        await state.set_state(AdminStates.rbot_token)
        await state.update_data(rbot_customer=profile.customer_id)
        await _ask(call, t("adm_rbot_ask_token"), t)
        return
    audit = {
        "stop": "reseller_bot.stopped_by_staff",
        "allow": "reseller_bot.allowed",
        "right": "reseller_bot.right",
    }
    if action == "stop":
        await resale.disconnect(session, profile, by_staff=True)
    elif action == "allow":
        await resale.allow(session, profile)
    elif action == "right":
        resale.set_bot_right(profile, resale.BOT_RIGHT not in (profile.permissions or []))
    if action in audit:
        await record(
            session,
            shop_id=shop.id,
            actor=Actor("staff", staff.id),
            action=audit[action],
            entity="reseller_profile",
            entity_id=profile.id,
            after={"permissions": list(profile.permissions or [])} if action == "right" else None,
        )
        await session.commit()
        await call.answer(t("adm_saved"))
    else:
        await call.answer()
    await staff_view(call, session, shop, t, profile)


@router.message(AdminStates.rbot_token, F.text, StaffOnly("resellers.manage"))
async def staff_token(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    cfg: dict[str, Any],
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    token = (message.text or "").strip()
    await _forget_token_message(message)
    data = await state.get_data()
    profile = await _reseller(session, shop, int(data.get("rbot_customer") or 0))
    if profile is None:
        await state.clear()
        await message.answer(t("adm_not_reseller"), reply_markup=menu(t, cfg, staff))
        return
    await resale.allow(session, profile)  # staff building it lifts their own earlier stop
    try:
        child = await resale.connect(session, profile, token, "")
    except resale.ResaleError as exc:
        await session.rollback()
        await message.answer(t(f"rb_err_{exc.key}"))
        return
    resale.set_bot_right(profile, True)  # the reseller runs it from here on
    await record(
        session,
        shop_id=shop.id,
        actor=Actor("staff", staff.id),
        action="reseller_bot.connected_by_staff",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"bot": child.bot_username},
    )
    await session.commit()
    await state.clear()
    await _point(child.id)
    who = await session.get(Customer, profile.customer_id)
    if who is not None and message.bot is not None:
        try:  # like a receipt: tell the reseller its bot is ready
            await message.bot.send_message(
                who.telegram_id,
                Texts(who.locale or shop.default_locale)(
                    "rb_ready_notice", bot=esc(child.bot_username or "")
                ),
            )
        except Exception:
            pass
    await message.answer(
        t("rb_connected", bot=esc(child.bot_username or "")), reply_markup=menu(t, cfg, staff)
    )
    await staff_view(message, session, shop, t, profile)
