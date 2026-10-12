"""Staff tools inside the bot. The web panel is the full admin UI; this covers on-the-go work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.auth import rbac
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin, Review
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import Btn, StaffOnly, clear_customer_input, show
from paurubot.core import customers, tickets
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import (
    ApplicationStatus,
    Broadcast,
    Customer,
    JobStatus,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    ResellerApplication,
    ResellerGroup,
    ResellerProfile,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine as payments
from paurubot.worker import queue

router = Router(name="admin")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())


VERIFIED_TAG = "verified"  # staff-set identity mark, kept in Customer.tags


def actor(staff: StaffIdentity) -> Actor:
    return Actor("staff", staff.id)


def home_kb(t: Texts, staff: StaffIdentity, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    from paurubot.bot.routers.admin_navigation import home_kb as navigation_kb

    return navigation_kb(t, staff, cfg)


@router.message(Btn("btn_admin"))
async def admin_home(
    message: Message, t: Texts, staff: StaffIdentity, cfg: dict[str, Any], state: FSMContext
) -> None:
    await state.clear()
    await message.answer(t("adm_nav_home_text"), reply_markup=menu(t, cfg, staff))


@router.callback_query(Admin.filter(F.section == "home"))
async def admin_home_cb(
    call: CallbackQuery, t: Texts, staff: StaffIdentity, cfg: dict[str, Any], state: FSMContext
) -> None:
    await state.clear()
    await call.answer()
    await show(call, t("adm_home"))
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_nav_home_text"), reply_markup=menu(t, cfg, staff))


def back_home(t: Texts) -> InlineKeyboardMarkup:
    return keyboards.back(t, Admin(section="home").pack())


@router.callback_query(Admin.filter(F.section == "stats"))
async def stats(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    await call.answer()
    now = datetime.now(UTC)
    day, month = now - timedelta(days=1), now - timedelta(days=30)

    async def scalar(stmt) -> int:  # type: ignore[no-untyped-def]
        return int(await session.scalar(stmt) or 0)

    sales = select(func.coalesce(func.sum(Order.amount), 0)).where(
        Order.shop_id == shop.id, Order.status == OrderStatus.COMPLETED
    )
    text = t(
        "adm_stats_text",
        customers=await scalar(
            select(func.count(Customer.id)).where(Customer.shop_id == shop.id, Customer.telegram_id > 0)
        ),
        customers_today=await scalar(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop.id, Customer.telegram_id > 0, Customer.created_at >= day
            )
        ),
        active=await scalar(
            select(func.count(Service.id)).where(
                Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE
            )
        ),
        sales_today=money(await scalar(sales.where(Order.completed_at >= day))),
        sales_month=money(await scalar(sales.where(Order.completed_at >= month))),
        pending=await scalar(
            select(func.count(Payment.id)).where(
                Payment.shop_id == shop.id, Payment.status == PaymentStatus.VERIFYING
            )
        ),
        tickets=await scalar(
            select(func.count(Ticket.id)).where(Ticket.shop_id == shop.id, Ticket.status == TicketStatus.OPEN)
        ),
    )
    await show(call, text, back_home(t))


# --- payments -------------------------------------------------------------------


def review_kb(t: Texts, payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_approve"), callback_data=Review(action="ok", id=payment_id).pack()
                ),
                InlineKeyboardButton(
                    text=t("btn_reject"), callback_data=Review(action="no", id=payment_id).pack()
                ),
            ]
        ]
    )


@router.callback_query(Admin.filter(F.section == "payments"), StaffOnly("payments.review"))
async def pending_payments(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    await call.answer()
    rows = list(
        await session.scalars(
            select(Payment)
            .where(Payment.shop_id == shop.id, Payment.status == PaymentStatus.VERIFYING)
            .order_by(Payment.id)
            .limit(10)
        )
    )
    if not rows:
        await show(call, t("adm_no_pending_payments"), back_home(t))
        return
    await show(call, t("adm_pending_payments", count=len(rows)), back_home(t))
    if not isinstance(call.message, Message):
        return
    for payment in rows:
        caption = receipt_caption(t, payment, payment.customer)
        if payment.receipt_file_id:
            await call.message.answer_photo(
                payment.receipt_file_id, caption=caption, reply_markup=review_kb(t, payment.id)
            )
        else:
            await call.message.answer(caption, reply_markup=review_kb(t, payment.id))


def receipt_caption(t: Texts, payment: Payment, customer: Customer, duplicate_of: int | None = None) -> str:
    text = t(
        "adm_receipt",
        id=payment.id,
        customer=esc(customer.display_name),
        telegram_id=customer.telegram_id,
        amount=money(payment.amount),
        order=payment.order_id or "-",
        note=esc(payment.receipt_text or "-"),
    )
    card = (payment.meta or {}).get("card")
    if card:
        text += "\n" + t("adm_receipt_card", last4=esc(str(card)))
    if customer.card_trust == "trusted":
        text += "\n" + t("adm_receipt_trusted")
    if duplicate_of or payment.duplicate_of_id:
        text += "\n\n" + t("adm_receipt_duplicate", other=duplicate_of or payment.duplicate_of_id)
    return text


async def _mark(call: CallbackQuery, suffix: str) -> None:
    message = call.message
    if not isinstance(message, Message):
        return
    body = f"{message.html_text}\n\n{suffix}"
    if message.caption is not None:
        await message.edit_caption(caption=body[:1024], reply_markup=None)
    else:
        await message.edit_text(body, reply_markup=None)


@router.callback_query(Review.filter(F.action == "ok"), StaffOnly("payments.review"))
async def approve(
    call: CallbackQuery,
    callback_data: Review,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    payment = await session.get(Payment, callback_data.id)
    if payment is None or payment.shop_id != shop.id:
        await call.answer(t("payment_not_found"), show_alert=True)
        return
    if not payments.manually_reviewable(payment):
        # Gateway payments settle with the gateway; only receipts and parked transfers are reviewed.
        await call.answer(t("payment_already_reviewed"), show_alert=True)
        return
    if not await payments.approve_manual(session, payment.id, staff.id):
        await call.answer(t("payment_already_reviewed"), show_alert=True)
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="payment.approved",
        entity="payment",
        entity_id=payment.id,
    )
    await session.commit()
    await payments.fulfill_one(session, payment.id)
    await call.answer(t("approved_short"))
    who = esc(call.from_user.full_name)
    await _mark(call, t("adm_approved_by", admin=who))


@router.callback_query(Review.filter(F.action == "no"), StaffOnly("payments.review"))
async def reject_ask(call: CallbackQuery, callback_data: Review, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.reject_reason)
    await state.update_data(
        payment_id=callback_data.id,
        review_chat=call.message.chat.id if call.message else None,
        review_message=call.message.message_id if call.message else None,
    )
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_reject_reason"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.reject_reason, F.text)
async def reject(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    payment = await session.get(Payment, int(data.get("payment_id") or 0))
    await state.clear()
    reason = "" if (message.text or "").strip() in {"-", "0"} else (message.text or "")[:200]
    if (
        payment is None
        or payment.shop_id != shop.id
        or not await payments.reject(session, payment.id, staff_id=staff.id, note=reason)
    ):
        await message.answer(t("payment_already_reviewed"), reply_markup=menu(t, cfg, staff))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="payment.rejected",
        entity="payment",
        entity_id=payment.id,
        after={"reason": reason},
    )
    await queue.enqueue(
        session, "notify", "payment_rejected", {"payment_id": payment.id, "reason": reason}, shop_id=shop.id
    )
    await message.answer(t("rejected_short"), reply_markup=menu(t, cfg, staff))


# --- tickets ------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "tickets"), StaffOnly("tickets.reply"))
async def open_tickets(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    await call.answer()
    rows = list(
        await session.scalars(
            select(Ticket)
            .where(Ticket.shop_id == shop.id, Ticket.status == TicketStatus.OPEN)
            .order_by(Ticket.updated_at)
            .limit(15)
        )
    )
    kb = InlineKeyboardBuilder()
    for ticket in rows:
        kb.row(
            InlineKeyboardButton(
                text=f"#{ticket.id} · {ticket.subject[:40]}",
                callback_data=Admin(section="ticket", id=ticket.id).pack(),
            )
        )
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="home").pack()))
    await show(call, t("adm_open_tickets", count=len(rows)), kb.as_markup())


@router.callback_query(Admin.filter(F.section == "ticket"), StaffOnly("tickets.reply"))
async def ticket_detail(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    ticket = await session.get(Ticket, callback_data.id)
    if ticket is None or ticket.shop_id != shop.id:
        await call.answer(t("ticket_not_found"), show_alert=True)
        return
    if callback_data.action == "close":
        await tickets.close(session, ticket)
        await call.answer(t("ticket_closed_ok"))
        return
    if callback_data.action == "reply":
        await call.answer()
        await state.set_state(AdminStates.ticket_reply)
        await state.update_data(ticket_id=ticket.id)
        if isinstance(call.message, Message):
            await call.message.answer(
                t("adm_ticket_reply_ask", id=ticket.id), reply_markup=keyboards.cancel(t)
            )
        return
    await call.answer()
    customer = await session.get(Customer, ticket.customer_id)
    lines = [
        t(
            "adm_ticket_title",
            id=ticket.id,
            subject=esc(ticket.subject),
            customer=esc(customer.display_name if customer else "-"),
        )
    ]
    for item in await tickets.messages(session, ticket.id):
        who = (
            t("ticket_support")
            if item.author_type == "staff"
            else esc(customer.display_name if customer else "?")
        )
        lines.append(f"<b>{who}</b>\n{esc(item.text or '')}{' 📎' if item.attachment_file_id else ''}")
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_ticket_reply"), callback_data=Admin(section="ticket", action="reply", id=ticket.id))
    kb.button(text=t("btn_ticket_close"), callback_data=Admin(section="ticket", action="close", id=ticket.id))
    kb.adjust(2)
    await show(call, "\n\n".join(lines)[-3900:], kb.as_markup())


@router.message(AdminStates.ticket_reply)
async def ticket_reply(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    ticket = await session.get(Ticket, int(data.get("ticket_id") or 0))
    await state.clear()
    if ticket is None or ticket.shop_id != shop.id:
        await message.answer(t("ticket_not_found"), reply_markup=menu(t, cfg, staff))
        return
    file_id = (
        message.photo[-1].file_id
        if message.photo
        else (message.document.file_id if message.document else None)
    )
    try:
        await tickets.add_message(
            session,
            ticket,
            author_type="staff",
            author_id=staff.id,
            text=message.text or message.caption,
            attachment_file_id=file_id,
            attachment_kind="photo" if message.photo else ("document" if file_id else None),
        )
    except tickets.TicketError as exc:
        await message.answer(t(exc.key), reply_markup=menu(t, cfg, staff))
        return
    await queue.enqueue(session, "notify", "ticket_reply", {"ticket_id": ticket.id}, shop_id=shop.id)
    await message.answer(t("adm_ticket_replied"), reply_markup=menu(t, cfg, staff))


# --- reseller applications ---------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "applications"), StaffOnly("resellers.manage"))
async def applications(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    if callback_data.action in {"approve", "reject"} and callback_data.id:
        application = await session.get(ResellerApplication, callback_data.id)
        if (
            application is None
            or application.shop_id != shop.id
            or application.status != ApplicationStatus.PENDING
        ):
            await call.answer(t("adm_application_done"), show_alert=True)
            return
        if callback_data.action == "approve":
            groups = list(
                await session.scalars(
                    select(ResellerGroup).where(
                        ResellerGroup.shop_id == shop.id, ResellerGroup.is_active.is_(True)
                    )
                )
            )
            if callback_data.page == 0:
                kb = InlineKeyboardBuilder()
                for group in groups:
                    kb.button(
                        text=f"{group.title} ({group.discount_percent}%)",
                        callback_data=Admin(
                            section="applications", action="approve", id=application.id, page=group.id
                        ),
                    )
                kb.adjust(1)
                await call.answer()
                await show(call, t("adm_choose_group") if groups else t("adm_no_groups"), kb.as_markup())
                return
            if callback_data.page not in {group.id for group in groups}:
                await call.answer(t("adm_no_groups"), show_alert=True)  # an old button or a forged id
                return
            existing = await session.scalar(
                select(ResellerProfile).where(ResellerProfile.customer_id == application.customer_id)
            )
            if existing is None:
                existing = ResellerProfile(
                    shop_id=shop.id,
                    customer_id=application.customer_id,
                    group_id=callback_data.page,
                    # The same rights an approval from the web panel gives.
                    permissions=sorted(rbac.DEFAULT_FOR_RESELLER),
                )
                session.add(existing)
                await session.flush()
            else:
                existing.group_id, existing.is_active = callback_data.page, True
            from paurubot.core.resellers import web_login

            login = await web_login(session, existing)
            web_username = login.username if login is not None else None
            application.status = ApplicationStatus.APPROVED
        else:
            web_username = None
            application.status = ApplicationStatus.REJECTED
        application.reviewed_by_staff_id = staff.id
        application.reviewed_at = datetime.now(UTC)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action=f"reseller.{callback_data.action}",
            entity="reseller_application",
            entity_id=application.id,
        )
        await queue.enqueue(
            session,
            "notify",
            "reseller_decision",
            {
                "customer_id": application.customer_id,
                "approved": callback_data.action == "approve",
                "web_username": web_username,
            },
            shop_id=shop.id,
        )
        await call.answer(t("adm_saved"))
        await show(call, t("adm_saved"), back_home(t))
        return
    await call.answer()
    rows = list(
        await session.scalars(
            select(ResellerApplication)
            .where(
                ResellerApplication.shop_id == shop.id,
                ResellerApplication.status == ApplicationStatus.PENDING,
            )
            .order_by(ResellerApplication.id)
            .limit(10)
        )
    )
    if not rows:
        await show(call, t("adm_no_applications"), back_home(t))
        return
    for application in rows:
        customer = await session.get(Customer, application.customer_id)
        kb = InlineKeyboardBuilder()
        kb.button(
            text=t("btn_approve"),
            callback_data=Admin(section="applications", action="approve", id=application.id),
        )
        kb.button(
            text=t("btn_reject"),
            callback_data=Admin(section="applications", action="reject", id=application.id),
        )
        if isinstance(call.message, Message):
            await call.message.answer(
                t(
                    "adm_application",
                    id=application.id,
                    customer=esc(customer.display_name if customer else "-"),
                    text=esc(application.text or "-"),
                ),
                reply_markup=kb.as_markup(),
            )


# --- customer lookup -----------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "lookup"), StaffOnly("customers.read"))
async def lookup_ask(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.lookup)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_lookup_ask"), reply_markup=keyboards.cancel(t))


async def customer_card(session: AsyncSession, t: Texts, customer: Customer, staff: StaffIdentity):  # type: ignore[no-untyped-def]
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    services = int(
        await session.scalar(
            select(func.count(Service.id)).where(
                Service.shop_id == customer.shop_id,
                Service.customer_id == customer.id,
                Service.status != ServiceStatus.DELETED,
            )
        )
        or 0
    )
    profile = customer.reseller_profile
    text = t(
        "adm_customer",
        name=esc(customer.display_name),
        telegram_id=customer.telegram_id,
        balance=money(balance),
        services=services,
        phone=esc(customer.phone or "-"),
        banned=t("yes") if customer.is_banned else t("no"),
        reseller=t("yes") if customer.is_reseller else t("no"),
    )
    text += "\n" + t(
        "adm_ua_status",
        kyc=t("adm_ua_kyc_yes") if VERIFIED_TAG in (customer.tags or []) else t("adm_ua_kyc_no"),
        card=t(f"adm_trust_{customer.card_trust}"),
        cron=t("adm_ua_off") if customer.alerts_muted else t("adm_ua_on"),
    )
    if profile is not None and profile.is_active:
        text += "\n" + t(
            "adm_ua_reseller_status",
            expires=jdate(profile.expires_at) if profile.expires_at else t("unlimited"),
            gb=money(profile.gb_price),
            hour=money(profile.hourly_price),
        )
    kb = InlineKeyboardBuilder()

    def button(key: str, section: str, action: str = "show", **params: Any) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=t(key, **params),
            callback_data=Admin(section=section, action=action, id=customer.id).pack(),
        )

    def pair(*buttons: InlineKeyboardButton | None) -> None:
        available = [b for b in buttons if b is not None]
        if available:
            kb.row(*available)

    write = staff.can("customers.write")
    card = staff.can("payments.review")
    # Rows follow the requested layout; each pair is (left, right) as Telegram draws it.
    pair(button("adm_user_btn_referrals", "uref"))
    if write:
        pair(
            button("adm_ua_btn_unref", "urefx", "unref") if customer.referrer_id else None,
            button("adm_ua_btn_clear_refs", "urefx", "clear"),
        )
    if card:
        pair(button("adm_ua_btn_card_on", "ucard", "on"))
    if write:
        pair(button("adm_ua_btn_kyc_on", "ukyc", "on"), button("adm_ua_btn_kyc_off", "ukyc", "off"))
    if card:
        pair(button("adm_ua_btn_card_off", "ucard", "off"))
    pair(
        button("adm_user_btn_give", "ugive") if staff.can("services.create") else None,
        button("adm_user_btn_trial", "utrial") if write else None,
    )
    can_move = write and staff.can("services.write") and staff.can("wallet.adjust")
    pair(
        button("adm_ud_payments", "upay") if staff.can("payments.read") else None,
        button("adm_ua_btn_move", "umove") if can_move and profile is None else None,
    )
    if staff.can("services.write"):
        pair(button("adm_ua_btn_power_off", "upower", "off"), button("adm_ua_btn_power_on", "upower", "on"))
    pair(
        button("adm_ua_btn_channels", "uchan"),
        button("adm_ua_btn_zero", "uzero") if staff.can("wallet.adjust") else None,
    )
    if write:
        pair(
            button(
                "adm_ua_btn_cron", "ucron", state=t("adm_ua_off") if customer.alerts_muted else t("adm_ua_on")
            )
        )
    reseller = profile is not None and profile.is_active and staff.can("resellers.manage")
    if reseller:
        pair(button("adm_user_btn_rbot", "rbot"), button("adm_ua_btn_rbot_del", "urbotdel"))
        pair(button("adm_ua_btn_base_gb", "ubase", "gb"), button("adm_ua_btn_base_hour", "ubase", "hour"))
    if write:
        pair(button("adm_ua_btn_hide_reseller" if reseller else "adm_user_btn_hide", "uhide"))
        pair(button("adm_ua_btn_hidden", "uhidden"))
    if reseller:
        pair(button("adm_ua_btn_expiry", "uexp"))
        pair(button("adm_ua_btn_demote", "udemote"))
        pair(button("adm_user_btn_group", "ugroup"))
    # The rest of the existing controls.
    pair(
        button("adm_user_btn_services", "usvc") if staff.can("services.read") else None,
        button("btn_adjust_balance", "adjust") if staff.can("wallet.adjust") else None,
    )
    pair(
        button("adm_user_btn_message", "umsg") if write else None,
        button("btn_unban" if customer.is_banned else "btn_ban", "ban")
        if staff.can("customers.ban")
        else None,
    )
    if card:
        pair(button("adm_user_btn_trust", "utrust", state=t(f"adm_trust_{customer.card_trust}")))
    pair(button("adm_ud_back_list", "uprofile"), button("adm_ud_admin", "home"))
    pair(button("adm_ud_close", "uprofile_close"))
    return text, kb.as_markup()


@router.message(AdminStates.lookup, F.text)
async def lookup(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    rows, _ = await customers.search(session, shop.id, message.text or "", limit=1)
    await state.clear()
    if not rows:
        # Not a customer: maybe a service's panel username or label (ADM-09).
        query = (message.text or "").strip().lstrip("@")[:64]
        service = (
            await session.scalar(
                select(Service)
                .where(
                    Service.shop_id == shop.id,
                    Service.status != ServiceStatus.DELETED,
                    (Service.panel_username == query) | (Service.label == query),
                )
                .order_by(Service.id.desc())
                .limit(1)
            )
            if query
            else None
        )
        if service is not None and staff.can("services.read"):
            from paurubot.bot.routers.admin_tools import service_kb, service_text

            await message.answer(t("adm_found"), reply_markup=menu(t, cfg, staff))
            await message.answer(service_text(t, service), reply_markup=service_kb(t, service, staff))
            return
        await message.answer(t("adm_customer_not_found"), reply_markup=menu(t, cfg, staff))
        return
    text, kb = await customer_card(session, t, rows[0], staff)
    await message.answer(t("adm_found"), reply_markup=menu(t, cfg, staff))
    await message.answer(text, reply_markup=kb)


@router.callback_query(Admin.filter(F.section == "ban"), StaffOnly("customers.ban"))
async def toggle_ban(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    customer = await customers.get(session, shop.id, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    await customers.set_ban(session, customer, not customer.is_banned, "admin", actor(staff))
    await call.answer(t("adm_saved"))
    text, kb = await customer_card(session, t, customer, staff)
    await show(call, text, kb)


@router.callback_query(Admin.filter(F.section == "adjust"), StaffOnly("wallet.adjust"))
async def adjust_ask(call: CallbackQuery, callback_data: Admin, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.adjust)
    await state.update_data(customer_id=callback_data.id)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_adjust_ask"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.adjust, F.text)
async def adjust(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    delta = parse_int(message.text)
    data = await state.get_data()
    customer = await customers.get(session, shop.id, int(data.get("customer_id") or 0))
    if delta is None or delta == 0 or customer is None:
        await message.answer(t("number_expected"))
        return
    await clear_customer_input(state)
    try:
        await wallet.adjust(
            session,
            shop.id,
            customer.id,
            delta,
            key=f"adjust:{message.chat.id}:{message.message_id}",
            actor=f"staff:{staff.id or message.chat.id}",
            memo="bot adjustment",
        )
    except wallet.InsufficientFunds:
        await message.answer(t("adm_adjust_too_much"), reply_markup=menu(t, cfg, staff))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="wallet.adjust",
        entity="customer",
        entity_id=customer.id,
        after={"delta": delta},
    )
    await queue.enqueue(
        session, "notify", "wallet_adjusted", {"customer_id": customer.id, "delta": delta}, shop_id=shop.id
    )
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


# --- broadcast & maintenance ---------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "broadcast"), StaffOnly("broadcast.send"))
async def broadcast_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    if callback_data.action == "send" and callback_data.id:
        row = await session.get(Broadcast, callback_data.id)
        if row is None or row.shop_id != shop.id or row.status != JobStatus.DRAFT:
            await call.answer(t("adm_application_done"), show_alert=True)
            return
        row.status = JobStatus.QUEUED
        await queue.enqueue(
            session,
            "broadcast",
            "run",
            {"broadcast_id": row.id},
            shop_id=shop.id,
            dedupe_key=f"broadcast:{row.id}",
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="broadcast.queued",
            entity="broadcast",
            entity_id=row.id,
        )
        await call.answer(t("adm_broadcast_queued"))
        await show(call, t("adm_broadcast_queued"))
        return
    await call.answer()
    await state.set_state(AdminStates.broadcast)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_broadcast_ask"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.broadcast)
async def broadcast_draft(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    row = Broadcast(
        shop_id=shop.id,
        created_by_staff_id=staff.id,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        status=JobStatus.DRAFT,
        audience={"kind": "all"},
    )
    session.add(row)
    await session.flush()
    count = int(
        await session.scalar(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop.id, Customer.is_banned.is_(False), Customer.blocked_bot_at.is_(None)
            )
        )
        or 0
    )
    from paurubot.bot.routers.admin_tools import broadcast_kb

    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(t("adm_bc_options", count=count), reply_markup=broadcast_kb(t, row))


@router.callback_query(Admin.filter(F.section == "maintenance"), StaffOnly("settings.write"))
async def maintenance(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
) -> None:
    enabled = not cfg["bot_enabled"]
    await shop_settings.set_many(session, shop.id, {"bot_enabled": enabled})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.maintenance",
        entity="shop",
        entity_id=shop.id,
        after={"bot_enabled": enabled},
    )
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    await call.answer(t("adm_saved"))
    await show(call, t("adm_home"), home_kb(t, staff, cfg))
