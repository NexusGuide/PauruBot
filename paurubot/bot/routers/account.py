"""Trials, referrals, reseller applications and support tickets."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Ticket as TicketCb
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ResellerStates, TicketStates
from paurubot.bot.utils import Btn, show
from paurubot.core import customers, tickets, trials
from paurubot.core.formatting import esc, jdate
from paurubot.db.models import ApplicationStatus, Customer, ResellerApplication, Ticket
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="account")


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
    count = await customers.referral_count(session, customer.id)
    await message.answer(
        t("referral_text", link=link, count=count, percent=cfg["referral_percent"]),
        disable_web_page_preview=True,
    )


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
async def ticket_new(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(TicketStates.subject)
    if isinstance(call.message, Message):
        await call.message.answer(t("ticket_ask_subject"), reply_markup=keyboards.cancel(t))


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
            ticket = await tickets.open_ticket(session, customer, data.get("subject") or "-")
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
