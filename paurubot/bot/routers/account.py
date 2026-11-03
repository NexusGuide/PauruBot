"""Trials, referrals, reseller applications and support tickets."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Nav
from paurubot.bot.callbacks import Ticket as TicketCb
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ResellerStates, TicketStates
from paurubot.bot.utils import Btn, show
from paurubot.core import customers, marketing, tickets, trials
from paurubot.core.formatting import esc, jdate
from paurubot.db.models import ApplicationStatus, Customer, ResellerApplication, Ticket
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="account")


def marketing_keyboard(t: Texts, opt_out: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("marketing_enable" if opt_out else "marketing_disable"),
                    callback_data="marketing:on" if opt_out else "marketing:off",
                )
            ]
        ]
    )


def marketing_text(t: Texts, opt_out: bool) -> str:
    return t("marketing_off" if opt_out else "marketing_on") + "\n\n" + t("marketing_hint")


@router.message(Command("marketing", "stop_ads", "start_ads"), F.chat.type == "private")
async def marketing_preferences(
    message: Message, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    command = (message.text or "").split()[0].split("@")[0]
    if command in {"/stop_ads", "/start_ads"}:
        await marketing.set_preference(session, customer, opt_out=command == "/stop_ads", source="bot")
        await session.commit()
    await message.answer(
        marketing_text(t, customer.marketing_opt_out),
        reply_markup=marketing_keyboard(t, customer.marketing_opt_out),
    )


@router.callback_query(F.data.in_({"marketing:on", "marketing:off"}))
async def marketing_choice(
    callback: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
        await callback.answer()
        return
    await marketing.set_preference(session, customer, opt_out=callback.data == "marketing:off", source="bot")
    await session.commit()
    # answer rather than edit: replaying an identical choice must not fail "message not modified".
    await callback.answer(t("marketing_saved"))
    await callback.message.answer(
        marketing_text(t, customer.marketing_opt_out),
        reply_markup=marketing_keyboard(t, customer.marketing_opt_out),
    )


@router.message(Btn("btn_trial"))
async def trial(message: Message, session: AsyncSession, customer: Customer, t: Texts) -> None:
    try:
        await trials.request(session, customer)
    except trials.TrialError as exc:
        await session.rollback()
        await message.answer(t(exc.key))
        return
    await session.commit()
    await message.answer(t("trial_processing"))


@router.message(Btn("btn_referral"))
async def referral(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    bot: Bot,
) -> None:
    if not cfg["referral_enabled"]:
        await message.answer(t("referral_disabled"))
        return
    username = shop.bot_username or (await bot.me()).username
    link = f"https://t.me/{username}?start=ref_{customer.telegram_id}"
    text, markup = await referral_screen(session, customer, t, cfg, link)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def reward_label(t: Texts, info: dict[str, Any]) -> str:
    reward = info["reward"]
    if reward["plan_title"]:
        return esc(str(reward["plan_title"]))
    return t("referral_config_size", gb=reward["gb"], days=reward["days"])


async def referral_screen(
    session: AsyncSession, customer: Customer, t: Texts, cfg: dict[str, Any], link: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The invite text: the cash share, a reward config with its progress, or both."""
    from paurubot.core import referral_rewards

    count = await customers.referral_count(session, customer.id)
    if not referral_rewards.config_on(cfg):
        return t("referral_text", link=link, count=count, percent=cfg["referral_percent"]), None
    lines = [t("referral_head", link=link, count=count)]
    if referral_rewards.cash_on(cfg):
        lines.append(t("referral_cash_line", percent=cfg["referral_percent"]))
    given = await referral_rewards.reward(session, customer.shop_id, cfg)
    info = referral_rewards.view(await referral_rewards.status(session, customer, cfg), given)
    markup = None
    if given is not None:
        rule = (
            "referral_config_rule_join" if cfg["referral_config_counts"] == "join" else "referral_config_rule"
        )
        lines.append(t(rule, every=info["every"], reward=reward_label(t, info)))
        lines.append(t("referral_config_progress", progress=info["progress"], every=info["every"]))
        if info["available"]:
            lines.append(t("referral_config_available", available=info["available"]))
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("btn_referral_claim"), callback_data=Nav(to="ref_claim").pack()
                        )
                    ]
                ]
            )
    return "\n\n".join(lines), markup


@router.callback_query(Nav.filter(F.to == "ref_claim"))
async def referral_claim(
    call: CallbackQuery,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    """Take one reward config for counted invites; it arrives like any purchase."""
    from paurubot.core import referral_rewards

    try:
        await referral_rewards.claim(session, customer, cfg)
    except referral_rewards.RewardError as exc:
        await session.rollback()
        await call.answer(t(exc.key), show_alert=True)
        return
    await session.commit()
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(t("referral_config_claimed"))


@router.message(Btn("btn_reseller"))
async def reseller(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    from paurubot.bot.routers.reseller import show_hub

    await state.clear()
    if await show_hub(message, session, customer, t):  # an active reseller gets its own panel (TEN-13)
        return
    from paurubot.bot.routers.reseller import membership_offer_message

    if await membership_offer_message(message, session, customer, t, cfg):  # TEN-16
        return
    if not cfg["reseller_applications_enabled"]:
        await message.answer(t("reseller_closed"))
        return
    pending = await session.scalar(
        select(ResellerApplication.id).where(
            ResellerApplication.customer_id == customer.id,
            ResellerApplication.status == ApplicationStatus.PENDING,
        )
    )
    if pending:
        await message.answer(t("reseller_application_pending"))
        return
    await state.set_state(ResellerStates.application)
    await message.answer(t("reseller_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResellerStates.application, F.text)
async def reseller_application(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    application = ResellerApplication(
        shop_id=customer.shop_id, customer_id=customer.id, text=(message.text or "")[:1000]
    )
    session.add(application)
    await session.flush()
    await queue.enqueue(
        session,
        "notify",
        "reseller_application",
        {"application_id": application.id},
        shop_id=customer.shop_id,
    )
    await state.clear()
    await message.answer(t("reseller_application_sent"), reply_markup=menu(t, cfg, staff))


# --- tickets ------------------------------------------------------------------


async def show_tickets(
    event: Message | CallbackQuery, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    items = await tickets.for_customer(session, customer)
    support = t.context.get("support")
    text = t("tickets_header") + (("\n" + t("support_contact", support=support)) if support else "")
    await show(event, text, keyboards.tickets_kb(t, items))


@router.message(Btn("btn_tickets"))
async def tickets_entry(
    message: Message, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await state.clear()
    await show_tickets(message, session, customer, t)


@router.callback_query(TicketCb.filter(F.action == "list"))
async def tickets_list(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    await show_tickets(call, session, customer, t)


@router.callback_query(TicketCb.filter(F.action == "new"))
async def ticket_new(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await call.answer()
    await state.clear()
    desks = await tickets.departments(session, customer.shop_id)
    if desks:  # the customer picks who the ticket is for (F-14)
        await show(call, t("ticket_ask_department"), keyboards.departments_kb(t, desks))
        return
    await _ask_subject(call, t, state)


async def _ask_subject(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await state.set_state(TicketStates.subject)
    if isinstance(call.message, Message):
        await call.message.answer(t("ticket_ask_subject"), reply_markup=keyboards.cancel(t))


@router.callback_query(TicketCb.filter(F.action == "dept"))
async def ticket_department(
    call: CallbackQuery,
    callback_data: TicketCb,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    desk = await tickets.department(session, customer.shop_id, callback_data.id)
    if desk is None:
        await call.answer(t("ticket_department_unknown"), show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.update_data(department_id=desk.id)
    await _ask_subject(call, t, state)


@router.message(TicketStates.subject, F.text)
async def ticket_subject(message: Message, t: Texts, state: FSMContext) -> None:
    await state.update_data(subject=(message.text or "")[:128])
    await state.set_state(TicketStates.message)
    await message.answer(t("ticket_ask_message"))


async def _own_ticket(session: AsyncSession, customer: Customer, ticket_id: int) -> Ticket | None:
    ticket = await session.get(Ticket, ticket_id)
    return ticket if ticket is not None and ticket.customer_id == customer.id else None


def _attachment(message: Message) -> tuple[str | None, str | None]:
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.document:
        return message.document.file_id, "document"
    return None, None


@router.message(TicketStates.message)
@router.message(TicketStates.reply)
async def ticket_message(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    file_id, kind = _attachment(message)
    text = message.text or message.caption
    if not text and not file_id:
        await message.answer(t("ticket_ask_message"))
        return
    data = await state.get_data()
    try:
        if data.get("ticket_id"):
            ticket = await _own_ticket(session, customer, int(data["ticket_id"]))
            if ticket is None:
                raise tickets.TicketError("ticket_not_found")
        else:
            ticket = await tickets.open_ticket(
                session,
                customer,
                data.get("subject") or "-",
                department_id=int(data["department_id"]) if data.get("department_id") else None,
            )
        await tickets.add_message(
            session,
            ticket,
            author_type="customer",
            author_id=customer.id,
            text=text,
            attachment_file_id=file_id,
            attachment_kind=kind,
        )
    except tickets.TicketError as exc:
        await state.clear()
        await message.answer(t(exc.key), reply_markup=menu(t, cfg, staff))
        return
    await queue.enqueue(
        session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=customer.shop_id
    )
    await state.clear()
    await message.answer(t("ticket_sent", id=ticket.id), reply_markup=menu(t, cfg, staff))


@router.callback_query(TicketCb.filter(F.action == "view"))
async def ticket_view(
    call: CallbackQuery, callback_data: TicketCb, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    ticket = await _own_ticket(session, customer, callback_data.id)
    if ticket is None:
        await call.answer(t("ticket_not_found"), show_alert=True)
        return
    await call.answer()
    lines = [
        t(
            "ticket_title",
            id=ticket.id,
            subject=esc(ticket.subject),
            status=t(f"ticket_status_{ticket.status.value}"),
        )
    ]
    for item in await tickets.messages(session, ticket.id):
        who = t("ticket_you") if item.author_type == "customer" else t("ticket_support")
        body = esc(item.text or "") + (" 📎" if item.attachment_file_id else "")
        lines.append(f"<b>{who}</b> · {jdate(item.created_at, with_time=True)}\n{body}")
    await show(
        call, "\n\n".join(lines)[-3900:], keyboards.ticket_kb(t, ticket.id, ticket.status.value == "closed")
    )


@router.callback_query(TicketCb.filter(F.action == "reply"))
async def ticket_reply(
    call: CallbackQuery,
    callback_data: TicketCb,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    ticket = await _own_ticket(session, customer, callback_data.id)
    if ticket is None:
        await call.answer(t("ticket_not_found"), show_alert=True)
        return
    await call.answer()
    await state.set_state(TicketStates.reply)
    await state.update_data(ticket_id=ticket.id)
    if isinstance(call.message, Message):
        await call.message.answer(t("ticket_ask_message"), reply_markup=keyboards.cancel(t))


@router.callback_query(TicketCb.filter(F.action == "close"))
async def ticket_close(
    call: CallbackQuery, callback_data: TicketCb, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    ticket = await _own_ticket(session, customer, callback_data.id)
    if ticket is None:
        await call.answer(t("ticket_not_found"), show_alert=True)
        return
    await tickets.close(session, ticket)
    await call.answer(t("ticket_closed_ok"))
    await show_tickets(call, session, customer, t)
