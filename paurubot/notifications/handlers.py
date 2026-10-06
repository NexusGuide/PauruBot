"""Notification queue handlers: what customers and staff are told, and when (SVC-05, SUP-03, GRW-06)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.auth import rbac
from paurubot.config import get_settings
from paurubot.core.formatting import esc, money, processing_key
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Broadcast,
    Customer,
    JobStatus,
    LogChannel,
    Order,
    OrderKind,
    Payment,
    ResellerApplication,
    Service,
    Shop,
    Staff,
    Ticket,
)
from paurubot.i18n import Texts, texts_for
from paurubot.notifications import telegram
from paurubot.worker import queue
from paurubot.worker.runner import Permanent, QueueSpec, register_queue

log = logging.getLogger(__name__)


async def _texts(session: AsyncSession, customer: Customer) -> Texts:
    shop = await session.get(Shop, customer.shop_id)
    return await texts_for(
        session,
        customer.shop_id,
        customer.locale or (shop.default_locale if shop else None),
        {
            "name": customer.first_name or "",
            "first_name": customer.first_name or "",
            "username": customer.username or "",
            "shop": shop.title if shop else "",
        },
    )


async def _to_customer(session: AsyncSession, customer: Customer, text: str, **kwargs: Any) -> None:
    try:
        await telegram.send_text(customer.telegram_id, text, **kwargs)
    except telegram.Undeliverable:
        customer.blocked_bot_at = utcnow()


async def staff_chats(session: AsyncSession, shop_id: int, permission: str) -> list[int]:
    chats = set()
    for staff in await session.scalars(
        select(Staff).where(
            Staff.shop_id == shop_id, Staff.is_active.is_(True), Staff.telegram_id.is_not(None)
        )
    ):
        if rbac.is_scoped(staff.role):
            continue  # resellers never receive shop-wide staff alerts
        if staff.telegram_id and rbac.allowed(staff.role, permission):
            chats.add(staff.telegram_id)
    chats.update(get_settings().owner_telegram_ids)
    return sorted(chats)


async def _to_staff(
    session: AsyncSession,
    shop_id: int,
    permission: str,
    text: str,
    *,
    photo: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    for chat_id in await staff_chats(session, shop_id, permission):
        try:
            if photo:
                await telegram.send_photo(chat_id, photo, caption=text[:1024], reply_markup=markup)
            else:
                await telegram.send_text(chat_id, text, reply_markup=markup)
        except telegram.Undeliverable:
            continue


async def report(session: AsyncSession, shop_id: int, topic: str, text: str) -> None:
    """Copy an event to the shop's log channel for that topic (SUP-03)."""
    channel = await session.scalar(
        select(LogChannel)
        .where(LogChannel.shop_id == shop_id, LogChannel.topic.in_([topic, "all"]))
        .order_by(LogChannel.topic != topic)
    )
    if channel is None:
        return
    try:
        await telegram.send_text(channel.chat_id, text, message_thread_id=channel.thread_id)
    except telegram.Undeliverable:
        log.warning("log channel %s for %s is unreachable", channel.chat_id, topic)


# --- handlers -------------------------------------------------------------------------


async def order_completed(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot import keyboards
    from paurubot.bot.views import qr_png, service_card

    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None or order.service_id is None:
        return
    service = await session.get(Service, order.service_id)
    customer = await session.get(Customer, order.customer_id)
    if service is None or customer is None:
        return
    t = await _texts(session, customer)
    from paurubot.core import settings as shop_settings
    from paurubot.core.orders import service_extras

    cfg = await shop_settings.load(session, customer.shop_id)
    heading = (
        t("service_ready") if order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL} else t("service_updated")
    )
    markup = keyboards.service_kb(
        t, service, extras=service_extras(cfg, service), upgrade=bool(cfg["upgrade_enabled"])
    )
    from paurubot.provisioning.subscription import customer_link

    link = customer_link(service, cfg)
    await _to_customer(
        session, customer, heading + "\n\n" + service_card(t, service, link), reply_markup=markup
    )
    if link and order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL}:
        try:
            await telegram.send_photo(
                customer.telegram_id,
                BufferedInputFile(qr_png(link), "qr.png"),
                caption=t("qr_caption"),
            )
        except telegram.Undeliverable:
            pass
    await report(
        session,
        order.shop_id,
        "orders",
        t(
            "report_order",
            kind=t(f"order_kind_{order.kind.value}"),
            customer=esc(customer.display_name),
            telegram_id=customer.telegram_id,
            amount=money(order.amount),
            username=esc(service.panel_username),
        ),
    )


async def order_failed(session: AsyncSession, job: queue.Claimed) -> None:
    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None:
        return
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    key = "trial_failed" if order.kind == OrderKind.TRIAL else "order_failed_refunded"
    await _to_customer(session, customer, t(key, amount=money(order.amount)))
    text = t(
        "adm_order_failed",
        id=order.id,
        customer=esc(customer.display_name),
        reason=esc(str(job.payload.get("reason", ""))[:200]),
    )
    await _to_staff(session, order.shop_id, "orders.read", text)
    await report(session, order.shop_id, "errors", text)


async def payment_fulfilled(session: AsyncSession, job: queue.Claimed) -> None:
    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None:
        return
    customer = await session.get(Customer, payment.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    text = t("payment_approved", amount=money(payment.amount))
    if int(job.payload.get("bonus") or 0) > 0:
        text += "\n" + t("payment_bonus", bonus=money(int(job.payload["bonus"])))
    if payment.order_id:
        if job.payload.get("order_settled"):
            order = await session.get(Order, payment.order_id)
            key, values = processing_key(order.params if order else None)
            text += "\n" + t(key, **values)
        else:
            text += "\n" + t("order_after_payment_failed")
    await _to_customer(session, customer, text)
    await report(
        session,
        payment.shop_id,
        "payments",
        t(
            "report_payment",
            id=payment.id,
            provider=payment.provider,
            amount=money(payment.amount),
            customer=esc(customer.display_name),
            telegram_id=customer.telegram_id,
        ),
    )


async def payment_rejected(session: AsyncSession, job: queue.Claimed) -> None:
    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None:
        return
    customer = await session.get(Customer, payment.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    reason = esc(str(job.payload.get("reason") or "")) or "-"
    await _to_customer(session, customer, t("payment_rejected", amount=money(payment.amount), reason=reason))


async def receipt_review(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.routers.admin import receipt_caption, review_kb

    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None:
        return
    customer = await session.get(Customer, payment.customer_id)
    shop = await session.get(Shop, payment.shop_id)
    if customer is None or shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    caption = receipt_caption(t, payment, customer, job.payload.get("duplicate_of"))
    await _to_staff(
        session,
        shop.id,
        "payments.review",
        caption,
        photo=payment.receipt_file_id,
        markup=review_kb(t, payment.id),
    )


def _renew_markup(t: Texts, service_id: int) -> InlineKeyboardMarkup:
    from paurubot.bot.callbacks import Svc

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_renew"), callback_data=Svc(action="renew", id=service_id).pack()
                ),
                InlineKeyboardButton(
                    text=t("btn_extra_volume"), callback_data=Svc(action="extra_gb", id=service_id).pack()
                ),
            ]
        ]
    )


async def service_alert(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.core.formatting import size

    service = await session.get(Service, int(job.payload["service_id"]))
    if service is None:
        return
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    alert = str(job.payload["alert"])
    name = esc(service.label or service.panel_username)
    params = {
        "name": name,
        "remaining": size(int(job.payload.get("remaining") or 0)),
        "days": job.payload.get("days", 0),
    }
    text = t(f"alert_{alert}", **params)
    if alert in {"removed_by_staff", "disabled_by_staff"}:
        reason = str(job.payload.get("reason") or "").strip()
        if reason:
            text += "\n" + t("alert_reason", reason=esc(reason))
        await _to_customer(session, customer, text)
        return
    markup = None if alert == "deleted" or service.is_trial else _renew_markup(t, service.id)
    await _to_customer(session, customer, text, reply_markup=markup)


async def simple_customer(session: AsyncSession, job: queue.Claimed) -> None:
    customer = await session.get(Customer, int(job.payload["customer_id"]))
    if customer is None:
        return
    t = await _texts(session, customer)
    if job.kind == "referral_joined":
        text = t("referral_joined", name=esc(str(job.payload.get("name", ""))))
    elif job.kind == "referral_reward":
        text = t("referral_reward", amount=money(int(job.payload["amount"])))
    elif job.kind == "wallet_adjusted":
        delta = int(job.payload["delta"])
        text = t("wallet_credited" if delta > 0 else "wallet_debited", amount=money(abs(delta)))
    elif job.kind == "reseller_decision":
        text = t("reseller_approved" if job.payload.get("approved") else "reseller_rejected")
    else:
        raise Permanent(f"unknown customer notification {job.kind}")
    await _to_customer(session, customer, text)


async def reseller_application(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.callbacks import Admin

    application = await session.get(ResellerApplication, int(job.payload["application_id"]))
    if application is None:
        return
    customer = await session.get(Customer, application.customer_id)
    shop = await session.get(Shop, application.shop_id)
    if customer is None or shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_approve"),
                    callback_data=Admin(section="applications", action="approve", id=application.id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("btn_reject"),
                    callback_data=Admin(section="applications", action="reject", id=application.id).pack(),
                ),
            ]
        ]
    )
    await _to_staff(
        session,
        shop.id,
        "resellers.manage",
        t(
            "adm_application",
            id=application.id,
            customer=esc(customer.display_name),
            text=esc(application.text or "-"),
        ),
        markup=markup,
    )


async def ticket_new_message(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.callbacks import Admin

    ticket = await session.get(Ticket, int(job.payload["ticket_id"]))
    if ticket is None:
        return
    shop = await session.get(Shop, ticket.shop_id)
    customer = await session.get(Customer, ticket.customer_id)
    if shop is None or customer is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_open_ticket"), callback_data=Admin(section="ticket", id=ticket.id).pack()
                )
            ]
        ]
    )
    await _to_staff(
        session,
        shop.id,
        "tickets.reply",
        t("adm_ticket_new", id=ticket.id, subject=esc(ticket.subject), customer=esc(customer.display_name)),
        markup=markup,
    )


async def ticket_reply(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.callbacks import Ticket as TicketCb

    ticket = await session.get(Ticket, int(job.payload["ticket_id"]))
    if ticket is None:
        return
    customer = await session.get(Customer, ticket.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_view_ticket"), callback_data=TicketCb(action="view", id=ticket.id).pack()
                )
            ]
        ]
    )
    await _to_customer(
        session,
        customer,
        t("ticket_answered", id=ticket.id, subject=esc(ticket.subject)),
        reply_markup=markup,
    )


async def report_job(session: AsyncSession, job: queue.Claimed) -> None:
    if job.shop_id is None:
        return
    shop = await session.get(Shop, job.shop_id)
    if shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    params = {k: esc(str(v)) for k, v in (job.payload.get("params") or {}).items()}
    await report(session, shop.id, str(job.payload["topic"]), t(str(job.payload["key"]), **params))


async def panel_alert(session: AsyncSession, job: queue.Claimed) -> None:
    """Panel down / back up, node changes and quota warnings, to panel staff and the log channel."""
    from paurubot.db.models import Panel

    panel = await session.get(Panel, int(job.payload["panel_id"]))
    if panel is None:
        return
    shop = await session.get(Shop, panel.shop_id)
    if shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    event = str(job.payload["event"])
    if event not in {"down", "up", "node_down", "node_up", "quota"}:
        raise Permanent(f"unknown panel event {event}")
    text = t(
        f"panel_alert_{event}",
        panel=esc(panel.name),
        node=esc(str(job.payload.get("node") or "")),
        error=esc(str(job.payload.get("error") or "-")),
        minutes=job.payload.get("minutes") or 0,
        percent=job.payload.get("percent") or 0,
    )
    await _to_staff(session, shop.id, "panels.read", text)
    await report(session, shop.id, "panels", text)


async def reseller_notice(session: AsyncSession, job: queue.Claimed) -> None:
    """Suspension, resumption and low-balance notices to a reseller, copied to the owners (TEN-06)."""
    from paurubot.db.models import ResellerProfile
    from paurubot.ledger.accounts import wallet_balance

    profile = await session.get(ResellerProfile, int(job.payload["profile_id"]))
    if profile is None:
        return
    customer = await session.get(Customer, profile.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    notice = str(job.payload["notice"])
    balance = await wallet_balance(session, profile.shop_id, profile.customer_id)
    values = {"balance": money(balance), "debt": money(profile.billing_debt)}
    if notice not in {"suspended", "resumed", "low_balance"}:
        raise Permanent(f"unknown reseller notice {notice}")
    await _to_customer(session, customer, t(f"reseller_{notice}", **values))
    if notice != "low_balance":
        await report(
            session,
            profile.shop_id,
            "resellers",
            t(f"report_reseller_{notice}", customer=esc(customer.display_name), **values),
        )


register_queue(
    "notify",
    QueueSpec(
        {
            "reseller_notice": reseller_notice,
            "panel_alert": panel_alert,
            "order_completed": order_completed,
            "order_failed": order_failed,
            "payment_fulfilled": payment_fulfilled,
            "payment_rejected": payment_rejected,
            "receipt_review": receipt_review,
            "service_alert": service_alert,
            "referral_joined": simple_customer,
            "referral_reward": simple_customer,
            "wallet_adjusted": simple_customer,
            "reseller_decision": simple_customer,
            "reseller_application": reseller_application,
            "ticket_new_message": ticket_new_message,
            "ticket_reply": ticket_reply,
            "report": report_job,
        },
        batch=20,
    ),
)


# --- broadcast (GRW-06) ---------------------------------------------------------------

BROADCAST_BATCH = 200
BROADCAST_RATE = 25  # messages per second, under Telegram's global limit


async def run_broadcast(session: AsyncSession, job: queue.Claimed) -> None:
    row = await session.get(Broadcast, int(job.payload["broadcast_id"]))
    if row is None or row.status in {JobStatus.DONE, JobStatus.CANCELED}:
        return
    bot = telegram.get_bot()
    if row.source_chat_id is not None and row.source_message_id is not None:
        source_chat, source_message = int(row.source_chat_id), int(row.source_message_id)

        def copier(chat_id: int) -> Callable[[], Awaitable[Any]]:
            return lambda: bot.copy_message(chat_id, source_chat, source_message)
    elif row.text:
        text = esc(row.text)
        markup = None
        if row.buttons:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=b["text"], url=b["url"])]
                    for b in row.buttons
                    if b.get("text") and b.get("url")
                ]
            )

        def copier(chat_id: int) -> Callable[[], Awaitable[Any]]:
            return lambda: bot.send_message(chat_id, text, reply_markup=markup)
    else:
        raise Permanent("broadcast has no content")
    row.status = JobStatus.RUNNING
    if not row.total:
        from sqlalchemy import func

        row.total = int(await session.scalar(select(func.count(Customer.id)).where(*_audience(row))) or 0)
    await session.commit()

    while True:
        batch = list(
            await session.scalars(
                select(Customer)
                .where(*_audience(row), Customer.id > row.cursor)
                .order_by(Customer.id)
                .limit(BROADCAST_BATCH)
            )
        )
        if not batch:
            break
        await session.refresh(row, ["status"])
        if row.status == JobStatus.CANCELED:
            return
        for customer in batch:
            chat_id = customer.telegram_id
            try:
                await telegram.deliver(copier(chat_id))
                row.sent += 1
            except telegram.Undeliverable:
                customer.blocked_bot_at = utcnow()
                row.failed += 1
            except Exception as exc:
                if "retry after" in str(exc):
                    await session.commit()
                    raise
                row.failed += 1
            row.cursor = customer.id
            await asyncio.sleep(1 / BROADCAST_RATE)
        await session.commit()
    row.status = JobStatus.DONE
    row.finished_at = utcnow()
    await session.commit()


def _audience(row: Broadcast) -> list[Any]:
    conditions: list[Any] = [
        Customer.shop_id == row.shop_id,
        Customer.is_banned.is_(False),
        Customer.blocked_bot_at.is_(None),
    ]
    audience = row.audience or {}
    if audience.get("resellers_only"):
        from paurubot.db.models import ResellerProfile

        conditions.append(
            Customer.id.in_(select(ResellerProfile.customer_id).where(ResellerProfile.is_active.is_(True)))
        )
    if audience.get("buyers_only"):
        conditions.append(Customer.id.in_(select(Service.customer_id)))
    if audience.get("locale"):
        conditions.append(or_(Customer.locale == audience["locale"], Customer.locale.is_(None)))
    return conditions


register_queue("broadcast", QueueSpec({"run": run_broadcast}, batch=1, lease_seconds=3600))
