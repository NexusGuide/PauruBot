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
    BroadcastPin,
    Customer,
    JobStatus,
    LogChannel,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentStatus,
    ResellerApplication,
    Service,
    ServiceStatus,
    Shop,
    Staff,
    StockItem,
    Ticket,
)
from paurubot.i18n import Texts, texts_for
from paurubot.notifications import review_sync, telegram
from paurubot.worker import queue
from paurubot.worker.runner import Permanent, QueueSpec, Retry, register_queue

log = logging.getLogger(__name__)


async def _texts(session: AsyncSession, customer: Customer) -> Texts:
    shop = await session.get(Shop, customer.shop_id)
    return await texts_for(
        session,
        customer.shop_id,
        customer.locale or (shop.default_locale if shop else None),
        {
            "name": esc(customer.first_name or ""),
            "first_name": esc(customer.first_name or ""),
            "username": esc(customer.username or ""),
            "shop": esc(shop.title if shop else ""),
        },
    )


async def _to_customer(session: AsyncSession, customer: Customer, text: str, **kwargs: Any) -> None:
    try:
        await telegram.send_text(customer.telegram_id, text, shop_id=customer.shop_id, **kwargs)
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
    shop = await session.get(Shop, shop_id)
    if shop is None or shop.reseller_profile_id is None:
        chats.update(get_settings().owner_telegram_ids)  # not in a reseller's own bot (TEN-04)
    return sorted(chats)


async def _to_staff(
    session: AsyncSession,
    shop_id: int,
    permission: str,
    text: str,
    *,
    photo: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
) -> list[Any]:
    """Send to every staff chat with the permission; returns the messages that went out."""
    sent: list[Any] = []
    for chat_id in await staff_chats(session, shop_id, permission):
        try:
            if photo:
                message = await telegram.send_photo(
                    chat_id, photo, caption=text[:1024], reply_markup=markup, shop_id=shop_id
                )
            else:
                message = await telegram.send_text(chat_id, text, reply_markup=markup, shop_id=shop_id)
        except telegram.Undeliverable:
            continue
        if message is not None:
            sent.append(message)
    return sent


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
        await telegram.send_text(channel.chat_id, text, message_thread_id=channel.thread_id, shop_id=shop_id)
    except telegram.Undeliverable:
        log.warning("log channel %s for %s is unreachable", channel.chat_id, topic)


# --- handlers -------------------------------------------------------------------------


async def _send_wireguard(session: AsyncSession, t: Texts, customer: Customer, service: Service) -> None:
    """WireGuard services arrive as a .conf file and its QR (PNL-08)."""
    from paurubot.bot.views import qr_png
    from paurubot.panels import PanelError
    from paurubot.provisioning import lifecycle

    try:
        items = await lifecycle.links(session, service)
    except PanelError:
        items = []
    if not items:
        await _to_customer(session, customer, t("wireguard_unavailable"))
        return
    name = esc(service.label or service.panel_username)
    try:
        await telegram.send_document(
            customer.telegram_id,
            BufferedInputFile(items[0].encode(), f"{service.panel_username}.conf"),
            caption=t("wireguard_caption", name=name),
            shop_id=customer.shop_id,
        )
        png = qr_png(items[0])
        if png is not None:
            await telegram.send_photo(
                customer.telegram_id, BufferedInputFile(png, "wireguard.png"), shop_id=customer.shop_id
            )
    except telegram.Undeliverable:
        customer.blocked_bot_at = utcnow()


async def _delivery_photo(
    t: Texts, service: Service, link: str, cfg: dict[str, Any]
) -> tuple[BufferedInputFile, str] | None:
    """The info card when it carries the QR (BOT-06), otherwise the bare QR, if one fits."""
    from paurubot.bot import infocard
    from paurubot.bot.views import qr_png

    if cfg["info_card_enabled"] and cfg["info_card_qr"]:
        try:
            image = await infocard.make(t, service, link, cfg, await telegram.bot_for(service.shop_id))
            name = esc(service.label or service.panel_username)
            return BufferedInputFile(image, "card.png"), t("infocard_caption", name=name)
        except (OSError, ValueError) as exc:
            log.warning("info card for service %s failed: %s", service.id, exc)
    png = qr_png(link)
    return (BufferedInputFile(png, "qr.png"), t("qr_caption")) if png is not None else None


async def order_completed(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot import keyboards
    from paurubot.bot.views import service_card

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
    if order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL}:
        heading = t("service_ready")
    elif order.kind == OrderKind.MOVE:
        heading = (
            t("service_moved")
            if service.sub_token and cfg["branded_sub_links"]
            else t("service_moved_new_link")
        )
    else:
        heading = t("service_updated")
    markup = keyboards.service_kb(
        t,
        service,
        extras=service_extras(cfg, service),
        upgrade=bool(cfg["upgrade_enabled"]),
        card=bool(cfg["info_card_enabled"]),
    )
    from paurubot.provisioning.subscription import customer_link

    link = customer_link(service, cfg)
    await _to_customer(
        session, customer, heading + "\n\n" + service_card(t, service, link), reply_markup=markup
    )
    new_link = order.kind == OrderKind.MOVE and not cfg["branded_sub_links"]
    from paurubot.panels.registry import delivery_of

    kind = delivery_of(service.panel.kind if service.panel else None)
    fresh = order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL} or order.kind == OrderKind.MOVE
    if kind == "wireguard" and fresh:
        await _send_wireguard(session, t, customer, service)
    elif (
        kind == "subscription" and link and (order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL} or new_link)
    ):
        delivered = await _delivery_photo(t, service, link, cfg)
        try:
            if delivered is not None:
                photo, caption = delivered
                await telegram.send_photo(
                    customer.telegram_id, photo, caption=caption, shop_id=customer.shop_id
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


async def account_ready(session: AsyncSession, job: queue.Claimed) -> None:
    """A panel admin account order finished (TEN-14): the card, and the login on a purchase."""
    from paurubot.bot.routers.accounts import account_kb, account_text
    from paurubot.core import panel_accounts
    from paurubot.db.models import PanelAccount

    order = await session.get(Order, int(job.payload["order_id"]))
    account = await session.get(PanelAccount, int(job.payload["account_id"]))
    if order is None or account is None:
        return
    customer = await session.get(Customer, account.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    heading = t("acc_ready" if order.kind == OrderKind.ACCOUNT_PURCHASE else "acc_updated")
    await _to_customer(
        session, customer, heading + "\n\n" + account_text(t, account), reply_markup=account_kb(t, account)
    )
    if order.kind == OrderKind.ACCOUNT_PURCHASE:
        await _to_customer(
            session,
            customer,
            t(
                "acc_login",
                url=esc(panel_accounts.login_url(account)),
                username=esc(account.username),
                password=esc(panel_accounts.password_of(account)),
            ),
            protect_content=True,
        )
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
            username=esc(account.username),
        ),
    )


async def account_alert(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.callbacks import Acc
    from paurubot.db.models import PanelAccount

    account = await session.get(PanelAccount, int(job.payload["account_id"]))
    if account is None:
        return
    customer = await session.get(Customer, account.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("acc_btn_open"), callback_data=Acc(action="view", id=account.id).pack()
                )
            ]
        ]
    )
    key = str(job.payload["alert"])
    await _to_customer(session, customer, t(key, username=esc(account.username)), reply_markup=markup)


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
    reason = str(job.payload.get("reason", ""))
    if "reseller_balance_low" in reason:
        # A sale in a reseller's own bot failed because the reseller's wallet ran dry (TEN-04).
        text = t(
            "adm_reseller_balance_low",
            id=order.id,
            amount=money(int((order.params or {}).get("wholesale") or 0)),
        )
    else:
        text = t(
            "adm_order_failed", id=order.id, customer=esc(customer.display_name), reason=esc(reason[:200])
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
    sent = await _to_staff(
        session,
        shop.id,
        "payments.review",
        caption,
        photo=payment.receipt_file_id,
        markup=review_kb(t, payment.id, fixable=payment.provider == "card"),
    )
    for message in sent:
        await review_sync.remember(payment.id, message)


async def review_decided(session: AsyncSession, job: queue.Claimed) -> None:
    """A receipt was approved or rejected somewhere: update every reviewer's copy (F-24)."""
    from paurubot.bot.routers.admin import receipt_caption

    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None:
        return
    key = review_sync.OUTCOMES.get(str(job.payload.get("outcome")))
    customer = await session.get(Customer, payment.customer_id)
    shop = await session.get(Shop, payment.shop_id)
    if key is None or customer is None or shop is None:
        return
    given = job.payload.get("refs")
    refs = list(given) if isinstance(given, list) else await review_sync.take(payment.id)
    if not refs:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    who = esc(str(job.payload.get("by") or "-"))
    body = f"{receipt_caption(t, payment, customer)}\n\n{t(key, admin=who)}"
    skip = job.payload.get("skip")
    left = await review_sync.edit_all(
        shop.id, refs, body, skip=(int(skip[0]), int(skip[1])) if skip else None
    )
    if left:
        if not isinstance(given, list):
            await review_sync.give_back(payment.id, left)
        raise Retry("telegram busy while updating review messages")


async def payment_fix_requested(session: AsyncSession, job: queue.Claimed) -> None:
    """Staff sent a card receipt back (F-23): tell the customer what to fix, with a resend button."""
    from paurubot.bot.callbacks import Receipt

    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None or payment.status != PaymentStatus.PENDING or payment.receipt_file_id:
        return  # already re-sent, expired or decided: the message would only confuse
    customer = await session.get(Customer, payment.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    key = "payment_fix_card" if job.payload.get("kind") == "card" else "payment_fix_amount"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_receipt_resend"),
                    callback_data=Receipt(action="resend", id=payment.id).pack(),
                )
            ]
        ]
    )
    await _to_customer(
        session,
        customer,
        t(key, amount=money(payment.amount), payment_id=payment.id),
        reply_markup=markup,
    )


async def card_verify(session: AsyncSession, job: queue.Claimed) -> None:
    """A customer registered a card (PAY-18): its photo to staff with approve / reject."""
    from paurubot.bot.callbacks import Admin
    from paurubot.db.models import VerifiedCard

    card = await session.get(VerifiedCard, int(job.payload["card_id"]))
    if card is None or card.status != "pending":
        return
    customer = await session.get(Customer, card.customer_id)
    shop = await session.get(Shop, card.shop_id)
    if customer is None or shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    caption = t(
        "adm_card_verify",
        customer=esc(customer.display_name),
        telegram_id=customer.telegram_id,
        card=esc(card.masked),
    )
    other_id = job.payload.get("other_customer_id")
    if other_id:
        other = await session.get(Customer, int(other_id))
        if other is not None:
            caption += "\n\n" + t(
                "adm_card_verify_other", other=esc(other.display_name), telegram_id=other.telegram_id
            )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_approve"),
                    callback_data=Admin(section="cardv", action="ok", id=card.id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("btn_reject"), callback_data=Admin(section="cardv", action="no", id=card.id).pack()
                ),
            ]
        ]
    )
    await _to_staff(session, shop.id, "payments.review", caption, photo=card.photo_file_id, markup=markup)


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
    alert = str(job.payload["alert"])
    if customer.alerts_muted and alert in {"low_volume", "expiring"}:
        return  # the customer turned reminders off (SVC-15); endings are still told
    t = await _texts(session, customer)
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


async def auto_renew_notice(session: AsyncSession, job: queue.Claimed) -> None:
    """Auto-renewal could not run (F-02): a short wallet, or a rule that had to stop."""
    from paurubot.bot.callbacks import Svc
    from paurubot.core import auto_renew

    service = await session.get(Service, int(job.payload["service_id"]))
    if service is None or service.status == ServiceStatus.DELETED or auto_renew.rule(service) is None:
        return  # turned off or gone since: nothing left to say
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    name = esc(service.label or service.panel_username)
    if job.payload.get("reason") == auto_renew.SHORT:
        text = t(
            "auto_renew_short",
            name=name,
            required=money(int(job.payload.get("required") or 0)),
            balance=money(int(job.payload.get("balance") or 0)),
        )
    else:
        text = t("auto_renew_stopped", name=name)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_auto_renew"), callback_data=Svc(action="auto", id=service.id).pack()
                )
            ]
        ]
    )
    await _to_customer(session, customer, text, reply_markup=markup)


async def ip_replaced(session: AsyncSession, job: queue.Claimed) -> None:
    """A new device took a connection slot and an older one was cut off (JOB-03)."""
    service = await session.get(Service, int(job.payload["service_id"]))
    if service is None or service.status == ServiceStatus.DELETED:
        return
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    text = t(
        "ip_replaced",
        name=esc(service.label or service.panel_username),
        dropped=esc(", ".join(job.payload.get("dropped") or [])),
        arrived=esc(", ".join(job.payload.get("arrived") or [])),
        limit=int(job.payload.get("limit") or 0),
    )
    await _to_customer(session, customer, text)


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
    elif job.kind == "transfer_received":
        text = t(
            "transfer_received",
            amount=money(int(job.payload["amount"])),
            name=esc(str(job.payload.get("name", ""))),
        )
    elif job.kind == "private_code":
        text = t("private_code", code=esc(str(job.payload["code"])))
    elif job.kind == "card_verify_result":
        text = t(
            "card_verify_approved" if job.payload.get("approved") else "card_verify_rejected",
            card=esc(str(job.payload.get("card", ""))),
        )
    elif job.kind == "reseller_decision":
        text = t("reseller_approved" if job.payload.get("approved") else "reseller_rejected")
        if job.payload.get("approved") and job.payload.get("web_username"):
            from paurubot.core.resellers import web_panel_url

            text += "\n\n" + t(
                "reseller_web_ready", url=esc(web_panel_url()), username=esc(str(job.payload["web_username"]))
            )
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


async def job_dead(session: AsyncSession, job: queue.Claimed) -> None:
    """A job ran out of retries (JOB-05): tell the errors log channel so nothing fails silently."""
    shop_ids = [job.shop_id] if job.shop_id else list(await session.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        shop = await session.get(Shop, shop_id)
        if shop is None:
            continue
        t = await texts_for(session, shop.id, shop.default_locale)
        text = t(
            "report_job_dead",
            id=job.payload.get("job_id"),
            queue=esc(str(job.payload.get("queue"))),
            kind=esc(str(job.payload.get("kind"))),
            error=esc(str(job.payload.get("error") or "-")),
        )
        await report(session, shop.id, "errors", text)


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


async def tier_up(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.core import growth
    from paurubot.core import settings as shop_settings

    customer = await session.get(Customer, int(job.payload["customer_id"]))
    if customer is None:
        return
    cfg = await shop_settings.load(session, customer.shop_id)
    tier = growth.tier_for(cfg.get("loyalty_tiers") or [], customer.total_spent)
    if tier is None:
        return
    t = await _texts(session, customer)
    await _to_customer(session, customer, t("tier_up", title=esc(tier["title"]), percent=tier["percent"]))


async def trust_post(session: AsyncSession, job: queue.Claimed) -> None:
    """Anonymous 'someone just bought' post in the shop's public channel (GRW-08)."""
    from paurubot import kv
    from paurubot.core import growth
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import Plan

    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None:
        return
    cfg = await shop_settings.load(session, order.shop_id)
    chat = cfg.get("trust_channel_id")
    if not chat:
        return
    allowed, _ = await kv.hit(f"trust:{order.shop_id}", 20, 3600)
    if not allowed:
        return  # keep the channel readable on busy hours
    customer = await session.get(Customer, order.customer_id)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    shop = await session.get(Shop, order.shop_id)
    t = await texts_for(
        session,
        order.shop_id,
        shop.default_locale if shop else None,
        {"shop": esc(shop.title if shop else "")},
    )
    key = "trust_renew" if order.kind == OrderKind.RENEW else "trust_purchase"
    text = t(
        key,
        name=esc(growth.masked(customer.first_name if customer else None)),
        plan=esc(plan.title if plan else t("order_kind_purchase")),
    )
    try:
        await telegram.send_text(int(chat), text, shop_id=order.shop_id)
    except telegram.Undeliverable:
        log.warning("trust channel post failed for shop %s", order.shop_id)


async def lottery_result(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.db.models import Lottery

    lottery = await session.get(Lottery, int(job.payload["lottery_id"]))
    if lottery is None:
        return
    winners = [int(x) for x in (lottery.result or {}).get("winners", [])]
    names = []
    for customer_id in winners:
        customer = await session.get(Customer, customer_id)
        if customer is None:
            continue
        names.append(customer.display_name)
        t = await _texts(session, customer)
        await _to_customer(
            session, customer, t("lottery_won", title=esc(lottery.title), prize=esc(lottery.prize))
        )
    shop = await session.get(Shop, lottery.shop_id)
    t = await texts_for(session, lottery.shop_id, shop.default_locale if shop else None)
    text = t(
        "adm_lottery_drawn",
        title=esc(lottery.title),
        winners=esc(", ".join(names) or "-"),
        entrants=int((lottery.result or {}).get("entrants", 0)),
    )
    await _to_staff(session, lottery.shop_id, "broadcast.send", text)
    await report(session, lottery.shop_id, "reports", text)


async def stock_delivered(session: AsyncSession, job: queue.Claimed) -> None:
    """Send the customer the ready-made config they bought (CAT-05)."""
    from paurubot.bot.views import qr_png
    from paurubot.core import stock

    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None or not order.params.get("stock_item_id"):
        return
    customer = await session.get(Customer, order.customer_id)
    item = await session.get(StockItem, int(order.params["stock_item_id"]))
    if customer is None or item is None:
        return
    t = await _texts(session, customer)
    config = stock.payload(item)
    if len(config) <= 3500:
        await _to_customer(session, customer, t("stock_ready") + "\n\n<code>" + esc(config) + "</code>")
        png = qr_png(config) if "\n" not in config and len(config) <= 1500 else None
        if png is not None:
            try:
                await telegram.send_photo(
                    customer.telegram_id,
                    BufferedInputFile(png, "qr.png"),
                    caption=t("qr_caption"),
                    shop_id=customer.shop_id,
                )
            except telegram.Undeliverable:
                pass
    else:
        try:
            await telegram.send_document(
                customer.telegram_id,
                BufferedInputFile(config.encode(), f"config-{order.id}.txt"),
                caption=t("stock_ready"),
                shop_id=customer.shop_id,
            )
        except telegram.Undeliverable:
            pass
    await report(
        session,
        order.shop_id,
        "orders",
        t(
            "report_order",
            kind=t("order_kind_stock"),
            customer=esc(customer.display_name),
            telegram_id=customer.telegram_id,
            amount=money(order.amount),
            username=f"#{item.id}",
        ),
    )


async def batch_done(session: AsyncSession, job: queue.Claimed) -> None:
    """When every order of a bulk purchase has finished, send all the links in one file (CAT-07)."""
    from paurubot import kv
    from paurubot.core import bulk
    from paurubot.core import settings as shop_settings
    from paurubot.provisioning.subscription import customer_link

    customer = await session.get(Customer, int(job.payload["customer_id"]))
    if customer is None:
        return
    batch = str(job.payload["batch"])
    rows, finished = await bulk.progress(session, customer.id, batch)
    if not finished or not await kv.set_once(f"batch:{batch}:done", 30 * 86400):
        return
    t = await _texts(session, customer)
    cfg = await shop_settings.load(session, customer.shop_id)
    lines: list[str] = []
    ready = failed = 0
    from paurubot.core import stock

    for order in rows:
        if order.status == OrderStatus.COMPLETED and order.params.get("stock_item_id"):
            item = await session.get(StockItem, int(order.params["stock_item_id"]))
            if item is not None:
                ready += 1
                lines.append(f"{order.params.get('label') or order.id}\t{stock.payload(item)}")
                continue
        service = await session.get(Service, order.service_id) if order.service_id else None
        if order.status != OrderStatus.COMPLETED or service is None:
            failed += 1
            continue
        ready += 1
        link = customer_link(service, cfg) or ""
        lines.append(f"{service.label or service.panel_username}\t{service.panel_username}\t{link}")
    text = t("batch_done", ready=ready, failed=failed)
    if lines:
        document = BufferedInputFile(("\n".join(lines) + "\n").encode(), f"services-{batch}.txt")
        try:
            await telegram.send_document(
                customer.telegram_id, document, caption=text, shop_id=customer.shop_id
            )
        except telegram.Undeliverable:
            pass
    else:
        await _to_customer(session, customer, text)
    await report(
        session,
        customer.shop_id,
        "orders",
        t("report_batch", customer=esc(customer.display_name), telegram_id=customer.telegram_id, n=ready),
    )


register_queue(
    "notify",
    QueueSpec(
        {
            "batch_progress": batch_done,
            "stock_delivered": stock_delivered,
            "tier_up": tier_up,
            "ip_replaced": ip_replaced,
            "trust_post": trust_post,
            "lottery_result": lottery_result,
            "reseller_notice": reseller_notice,
            "panel_alert": panel_alert,
            "job_dead": job_dead,
            "order_completed": order_completed,
            "account_ready": account_ready,
            "account_alert": account_alert,
            "order_failed": order_failed,
            "payment_fulfilled": payment_fulfilled,
            "payment_rejected": payment_rejected,
            "receipt_review": receipt_review,
            "review_decided": review_decided,
            "payment_fix_requested": payment_fix_requested,
            "service_alert": service_alert,
            "auto_renew_notice": auto_renew_notice,
            "referral_joined": simple_customer,
            "referral_reward": simple_customer,
            "wallet_adjusted": simple_customer,
            "reseller_decision": simple_customer,
            "transfer_received": simple_customer,
            "card_verify_result": simple_customer,
            "private_code": simple_customer,
            "card_verify": card_verify,
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


async def _pin(bot: Any, chat_id: int, message_id: int) -> bool:
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    except Exception:  # a pin is a nicety; the message itself was delivered
        log.debug("could not pin broadcast in %s", chat_id)
        return False
    return True


async def run_unpin(session: AsyncSession, job: queue.Claimed) -> None:
    """Unpin broadcasts whose time is up, or all of a shop's with {"all": true} (GRW-06)."""
    shop_id = job.shop_id
    if shop_id is None:
        raise Permanent("unpin without a shop")
    bot = await telegram.bot_for(shop_id)
    if bot is None:
        return
    stmt = select(BroadcastPin).where(BroadcastPin.shop_id == shop_id)
    if not job.payload.get("all"):
        stmt = stmt.where(BroadcastPin.unpin_at.is_not(None), BroadcastPin.unpin_at <= utcnow())
    while True:
        rows = list(await session.scalars(stmt.order_by(BroadcastPin.id).limit(BROADCAST_BATCH)))
        if not rows:
            break
        for pin in rows:
            try:
                await bot.unpin_chat_message(pin.chat_id, message_id=pin.message_id)
            except Exception:  # already unpinned, or the customer blocked the bot
                log.debug("could not unpin %s in %s", pin.message_id, pin.chat_id)
            await session.delete(pin)
            await asyncio.sleep(1 / BROADCAST_RATE)
        await session.commit()


async def run_broadcast(session: AsyncSession, job: queue.Claimed) -> None:
    row = await session.get(Broadcast, int(job.payload["broadcast_id"]))
    if row is None or row.status in {JobStatus.DONE, JobStatus.CANCELED}:
        return
    bot = await telegram.bot_for(row.shop_id)
    if bot is None:
        raise Permanent("this shop's bot is stopped")
    markup = None
    if row.buttons:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=b["text"], url=b["url"])]
                for b in row.buttons
                if b.get("text") and b.get("url")
            ]
        )
    pin = bool((row.audience or {}).get("pin"))
    pin_hours = int((row.audience or {}).get("pin_hours") or 0)
    forward = bool((row.audience or {}).get("forward"))
    if row.source_chat_id is not None and row.source_message_id is not None:
        source_chat, source_message = int(row.source_chat_id), int(row.source_message_id)

        def copier(chat_id: int) -> Callable[[], Awaitable[Any]]:
            if forward:  # shows who sent it; buttons can't ride on a forward
                return lambda: bot.forward_message(chat_id, source_chat, source_message)
            return lambda: bot.copy_message(chat_id, source_chat, source_message, reply_markup=markup)
    elif row.text:
        text = esc(row.text)

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
                sent = await telegram.deliver(copier(chat_id))
                row.sent += 1
                if pin and sent is not None and await _pin(bot, chat_id, int(sent.message_id)):
                    from datetime import timedelta

                    session.add(
                        BroadcastPin(
                            shop_id=row.shop_id,
                            broadcast_id=row.id,
                            chat_id=chat_id,
                            message_id=int(sent.message_id),
                            unpin_at=utcnow() + timedelta(hours=pin_hours) if pin_hours else None,
                        )
                    )
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


def audience_conditions(shop_id: int, kind: str = "all") -> list[Any]:
    """Who a broadcast or a mass gift reaches (GRW-06, ADM-09)."""
    from paurubot.db.models import ResellerProfile

    conditions: list[Any] = [
        Customer.shop_id == shop_id,
        Customer.is_banned.is_(False),
        Customer.blocked_bot_at.is_(None),
        Customer.telegram_id > telegram.GUEST_CHAT_LIMIT,  # web guests have no chat
    ]
    if kind == "resellers":
        conditions.append(
            Customer.id.in_(select(ResellerProfile.customer_id).where(ResellerProfile.is_active.is_(True)))
        )
    elif kind == "buyers":
        conditions.append(Customer.id.in_(select(Service.customer_id).where(Service.is_trial.is_(False))))
    elif kind == "active":
        conditions.append(
            Customer.id.in_(
                select(Service.customer_id).where(
                    Service.status == ServiceStatus.ACTIVE, Service.is_trial.is_(False)
                )
            )
        )
    elif kind == "inactive":
        # Had a service, but none of them was used in the last week.
        from datetime import timedelta

        week_ago = utcnow() - timedelta(days=7)
        conditions.append(
            Customer.id.in_(select(Service.customer_id).where(Service.status != ServiceStatus.DELETED))
        )
        conditions.append(
            Customer.id.not_in(select(Service.customer_id).where(Service.online_at >= week_ago))
        )
    elif kind == "never_bought":
        conditions.append(Customer.id.not_in(select(Service.customer_id).where(Service.is_trial.is_(False))))
    return conditions


def _audience(row: Broadcast) -> list[Any]:
    audience = row.audience or {}
    kind = str(audience.get("kind") or "all")
    if audience.get("resellers_only"):
        kind = "resellers"
    elif audience.get("buyers_only") and kind == "all":
        kind = "buyers"
    conditions = audience_conditions(row.shop_id, kind)
    if audience.get("locale"):
        conditions.append(or_(Customer.locale == audience["locale"], Customer.locale.is_(None)))
    return conditions


async def run_mass_gift(session: AsyncSession, job: queue.Claimed) -> None:
    """The same wallet gift for everyone in an audience (ADM-09). Each credit has its own ledger
    key, so a retried job never pays anyone twice; progress is kept in the job payload."""
    from paurubot.ledger import wallet

    data = dict(job.payload)
    batch, amount, kind = str(data["batch"]), int(data["amount"]), str(data["audience"])
    shop_id = job.shop_id
    if shop_id is None or amount <= 0:
        raise Permanent("gift without a shop or an amount")
    cursor = int(data.get("cursor") or 0)
    paid = 0
    while True:
        rows = list(
            await session.scalars(
                select(Customer)
                .where(*audience_conditions(shop_id, kind), Customer.id > cursor)
                .order_by(Customer.id)
                .limit(BROADCAST_BATCH)
            )
        )
        if not rows:
            break
        for customer in rows:
            posted = await wallet.reward(
                session,
                shop_id,
                customer.id,
                amount,
                key=f"gift:{batch}:{customer.id}",
                memo="gift from the shop",
            )
            cursor = customer.id
            if not posted.created:
                continue  # paid on an earlier attempt, and already told
            paid += 1
            t = await _texts(session, customer)
            try:
                await telegram.send_text(
                    customer.telegram_id, t("gift_received", amount=money(amount)), shop_id=shop_id
                )
            except telegram.Undeliverable:
                customer.blocked_bot_at = utcnow()
            await asyncio.sleep(1 / BROADCAST_RATE)
        await queue.update_payload(session, job, {**data, "cursor": cursor})
        await session.commit()
    log.info("gift %s paid %s customers %s each", batch, paid, amount)


register_queue(
    "broadcast",
    QueueSpec({"run": run_broadcast, "gift": run_mass_gift, "unpin": run_unpin}, batch=1, lease_seconds=3600),
)
