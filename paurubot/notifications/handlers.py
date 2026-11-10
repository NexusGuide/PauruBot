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
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, jdate, local, money, processing_key
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
    TicketDepartment,
)
from paurubot.i18n import Texts, texts_for
from paurubot.logs import error_shop_id
from paurubot.notifications import error_reports, review_sync, telegram
from paurubot.security.redact import redact
from paurubot.worker import queue
from paurubot.worker.runner import Permanent, QueueSpec, Retry, register_queue

log = logging.getLogger(__name__)

TRUST_FAIL_TTL = 900  # how long a failing trust channel stays flagged for the admin panel


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


async def _to_customer(session: AsyncSession, customer: Customer, text: str, **kwargs: Any) -> Any:
    """Send to the customer; the sent message, or None when they cannot be reached."""
    try:
        return await telegram.send_text(customer.telegram_id, text, shop_id=customer.shop_id, **kwargs)
    except telegram.Undeliverable:
        customer.blocked_bot_at = utcnow()
        return None


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
    if shop is None or (shop.reseller_profile_id is None and shop.own_account_id is None):
        chats.update(get_settings().owner_telegram_ids)  # not in a reseller's own bot (TEN-04, OPB)
    return sorted(chats)


async def _muted(session: AsyncSession, shop_id: int, key: str) -> bool:
    return key in ((await shop_settings.load(session, shop_id)).get("alerts_muted") or [])


async def _to_staff(
    session: AsyncSession,
    shop_id: int,
    permission: str,
    text: str,
    *,
    photo: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
    event: str,
) -> list[Any]:
    """Send to every staff chat with the permission; returns the messages that went out.
    Nothing goes when the shop muted this `event` for staff chats (settings › alerts)."""
    if await _muted(session, shop_id, f"dm:{event}"):
        return []
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


# A newer topic goes where the broader one goes until the shop gives it a chat of its own.
TOPIC_FALLBACK = {"trials": "orders", "referrals": "payments"}


async def report(
    session: AsyncSession,
    shop_id: int,
    topic: str,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Copy an event to the shop's log channel for that topic (SUP-03), unless the shop muted it."""
    if await _muted(session, shop_id, f"group:{topic}"):
        return
    order = [topic, *([TOPIC_FALLBACK[topic]] if topic in TOPIC_FALLBACK else []), "all"]
    rows = list(
        await session.scalars(
            select(LogChannel).where(LogChannel.shop_id == shop_id, LogChannel.topic.in_(order))
        )
    )
    if not rows:
        return
    channel = min(rows, key=lambda row: order.index(row.topic))
    try:
        await telegram.send_text(
            channel.chat_id, text, message_thread_id=channel.thread_id, reply_markup=markup, shop_id=shop_id
        )
    except telegram.Undeliverable:
        log.warning("log channel %s for %s is unreachable", channel.chat_id, topic)
    except Exception:
        # A report is a copy for the shop's staff: one Telegram refuses (a removed forum thread,
        # flood control) must not fail the job and send the customer's own message again.
        log.warning("report to %s for %s was not sent", channel.chat_id, topic, exc_info=True)


async def _quietly(work: Awaitable[Any]) -> None:
    """Build and send a report without ever failing the customer's notification around it."""
    try:
        await work
    except Exception:
        log.warning("a report could not be built", exc_info=True)


async def report_error(session: AsyncSession, shop_id: int | None, text: str) -> None:
    """Never infer a tenant or enumerate shops for an operational error.

    Unscoped, legacy and deleted-shop errors are platform-only, using the main bot and
    explicitly configured OWNER_TELEGRAM_IDS. No owners configured means no Telegram
    delivery; the process's normal log sinks remain authoritative.
    """
    scoped_id = error_shop_id(shop_id)
    if scoped_id is not None and await session.get(Shop, scoped_id) is not None:
        await report(session, scoped_id, "errors", text)
        return
    for chat_id in sorted(set(get_settings().owner_telegram_ids)):
        try:
            await telegram.send_text(chat_id, text, shop_id=None)
        except Exception:  # delivery is best-effort and must not create another error alert
            log.warning("platform error notification could not be delivered to %s", chat_id)


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
    if service.customer_id != order.customer_id or service.shop_id != order.shop_id:
        raise ValueError("order/service ownership mismatch; delivery refused")
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
    from paurubot.notifications import log_reports

    async def order_report() -> None:
        topic, text, manage = await log_reports.service_order(session, order, service, customer)
        await report(session, order.shop_id, topic, text, markup=manage)

    await _quietly(order_report())


async def account_ready(session: AsyncSession, job: queue.Claimed) -> None:
    """A panel admin account order finished (TEN-14): the card, and the login on a purchase."""
    from paurubot.bot.routers.accounts import account_kb, account_text, login_message
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
        text, kb = login_message(t, account, panel_accounts.password_of(account))
        await _to_customer(session, customer, text, reply_markup=kb)
    from paurubot.core.formatting import days_left
    from paurubot.notifications import log_reports

    async def account_report() -> None:
        topic, text, manage = await log_reports.order_report(
            session,
            order,
            customer,
            config=account.username,
            panel=account.panel,
            gb=int(account.data_limit // log_reports.GB) if account.data_limit else 0,
            days=days_left(account.expire_at) or 0,
            plan_title=account.plan.title if account.plan else None,
        )
        await report(session, order.shop_id, topic, text, markup=manage)

    await _quietly(account_report())


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
    # Its dedicated bot (OPB), if any: the reseller hears it there too, and the main shop's staff.
    shop = await session.scalar(select(Shop).where(Shop.own_account_id == account.id))
    if shop is not None:
        await _own_alert(session, shop, account, customer, key)


async def _own_alert(session: AsyncSession, shop: Shop, account: Any, owner: Customer, key: str) -> None:
    """One alert about a dedicated bot to its reseller (in that bot) and the main shop's staff."""
    t = await texts_for(session, shop.id, owner.locale or shop.default_locale)
    bot = f"@{shop.bot_username}" if shop.bot_username else shop.title
    text = t(key, username=esc(account.username), bot=esc(bot))
    await _to_staff(session, shop.id, "dashboard.view", text, event="own_bot")
    if shop.parent_id is not None:
        parent_t = await _texts(session, owner)
        copy = parent_t("own_alert_admin", bot=esc(bot), owner=esc(owner.display_name), text=text)
        await _to_staff(session, shop.parent_id, "resellers.manage", copy, event="own_bot")


async def own_panel_alert(session: AsyncSession, job: queue.Claimed) -> None:
    """A dedicated bot stopped or started selling (OPB)."""
    from paurubot.db.models import PanelAccount

    shop = await session.get(Shop, int(job.payload["shop_id"]))
    if shop is None or shop.own_account_id is None:
        return
    account = await session.get(PanelAccount, shop.own_account_id)
    owner = await session.get(Customer, account.customer_id) if account is not None else None
    if account is None or owner is None:
        return
    await _own_alert(session, shop, account, owner, str(job.payload["alert"]))


async def account_changed(session: AsyncSession, job: queue.Claimed) -> None:
    """Staff changed a panel admin account: tell its owner what, why, and the card; a new
    password comes with the login."""
    from paurubot.bot.routers.accounts import account_kb, account_text, login_message
    from paurubot.core import panel_accounts
    from paurubot.db.models import PanelAccount

    account = await session.get(PanelAccount, int(job.payload["account_id"]))
    if account is None:
        return
    customer = await session.get(Customer, account.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    change = str(job.payload.get("change") or "")
    lines = [t(f"acc_staff_{change}", username=esc(account.username))]
    reason = str(job.payload.get("reason") or "").strip()
    if reason:
        lines.append(t("acc_staff_reason", reason=esc(reason)))
    lines += ["", account_text(t, account)]
    await _to_customer(session, customer, "\n".join(lines), reply_markup=account_kb(t, account))
    if job.payload.get("login"):
        text, kb = login_message(t, account, panel_accounts.password_of(account))
        await _to_customer(session, customer, text, reply_markup=kb)


async def account_auto_renew(session: AsyncSession, job: queue.Claimed) -> None:
    """An admin account's auto-renewal is waiting for money, or has stopped."""
    from paurubot.bot.routers.accounts import account_kb
    from paurubot.db.models import PanelAccount

    account = await session.get(PanelAccount, int(job.payload["account_id"]))
    if account is None:
        return
    customer = await session.get(Customer, account.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    reason = str(job.payload.get("reason") or "")
    if reason == "insufficient_balance":
        text = t(
            "acc_auto_short",
            username=esc(account.username),
            balance=money(int(job.payload.get("balance") or 0)),
            required=money(int(job.payload.get("required") or 0)),
        )
    else:
        why = t(reason)
        text = t("acc_auto_stopped", username=esc(account.username), reason=esc(why if why != reason else ""))
    await _to_customer(session, customer, text, reply_markup=account_kb(t, account))


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
    await _to_staff(session, order.shop_id, "orders.read", text, event="order_failed")
    from paurubot.notifications import log_reports

    async def failure_report() -> None:
        full = text
        if "reseller_balance_low" not in reason:
            full = await log_reports.order_failed(session, order, customer, redact(reason))
        markup = await log_reports.manage_markup(session, order.shop_id, customer)
        await report(session, order.shop_id, "errors", full, markup=markup)

    await _quietly(failure_report())


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
    from paurubot.notifications import log_reports

    async def paid_report() -> None:
        text = await log_reports.payment_paid(session, payment, customer)
        markup = await log_reports.manage_markup(session, payment.shop_id, customer)
        await report(session, payment.shop_id, "payments", text, markup=markup)

    await _quietly(paid_report())


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
    from paurubot.notifications import log_reports

    async def rejected_report() -> None:
        why = str(job.payload.get("reason") or "")
        text = await log_reports.payment_rejected(session, payment, customer, why)
        markup = await log_reports.manage_markup(session, payment.shop_id, customer)
        await report(session, payment.shop_id, "payments", text, markup=markup)

    await _quietly(rejected_report())


async def payment_voided(session: AsyncSession, job: queue.Claimed) -> None:
    """Staff voided an approved payment (REF-01): say what was taken back and why."""
    payment = await session.get(Payment, int(job.payload["payment_id"]))
    if payment is None or payment.status != PaymentStatus.VOIDED:
        return
    customer = await session.get(Customer, payment.customer_id)
    if customer is None:
        return
    t = await _texts(session, customer)
    info = (payment.meta or {}).get("void") or {}
    lines = [
        t(
            "payment_voided",
            id=payment.id,
            amount=money(payment.amount),
            reason=t(f"void_reason_{info.get('reason') or 'other'}"),
        )
    ]
    if info.get("service_id"):
        lines.append(t("payment_voided_service"))
    lines.append(t("payment_voided_support"))
    await _to_customer(session, customer, "\n\n".join(lines))


async def stars_unmatched(session: AsyncSession, job: queue.Claimed) -> None:
    """Stars charges the reconciliation could not tie to a payment (SEC-F05): staff decide."""
    shop = await session.get(Shop, job.shop_id) if job.shop_id is not None else None
    if shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    charges = "\n".join(f"<code>{esc(str(c))}</code>" for c in job.payload.get("charges") or [])
    await report(session, shop.id, "payments", t("report_stars_unmatched", charges=charges))


async def card_sms(session: AsyncSession, job: queue.Claimed) -> None:
    """What a bank deposit SMS did (REF-02), in the payments log channel."""
    shop = await session.get(Shop, job.shop_id) if job.shop_id is not None else None
    if shop is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    data = job.payload
    rial = int(data.get("amount_rial") or 0)
    values: dict[str, Any] = {
        "amount": money(rial // 10),
        "rial": money(rial),
        "id": data.get("payment_id") or "-",
        "count": int(data.get("candidates") or 0),
        "text": esc(str(data.get("text") or "")),
    }
    outcome = str(data.get("outcome"))
    if outcome not in {"approved", "unmatched", "ambiguous"}:
        raise Permanent(f"unknown card sms outcome {outcome}")
    await report(session, shop.id, "payments", t(f"report_card_sms_{outcome}", **values))


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
        event="receipt",
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
    await _to_staff(
        session,
        shop.id,
        "payments.review",
        caption,
        photo=card.photo_file_id,
        markup=markup,
        event="card_verify",
    )


def _renew_markup(t: Texts, service_id: int) -> InlineKeyboardMarkup:
    from paurubot.bot.callbacks import Svc, Wallet

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_renew"), callback_data=Svc(action="renew", id=service_id).pack()
                ),
                InlineKeyboardButton(
                    text=t("btn_extra_volume"), callback_data=Svc(action="extra_gb", id=service_id).pack()
                ),
            ],
            # Paying needs money first; a short wallet is the usual reason a reminder is ignored.
            [InlineKeyboardButton(text=t("btn_topup"), callback_data=Wallet(action="topup").pack())],
        ]
    )


# Reminders about one service that a newer one makes stale (REF-05): only the latest is kept.
REPLACEABLE_ALERTS = frozenset({"low_volume", "expiring", "expired", "limited"})
ALERT_MESSAGE_TTL = 30 * 86400


async def _replace_alert(customer: Customer, service_id: int, message: Any) -> None:
    """Remember the new alert message and delete the one it replaces. Best effort: a message
    Telegram no longer lets the bot delete (over 48 hours old) simply stays."""
    from paurubot import kv

    message_id = getattr(message, "message_id", None)
    if not isinstance(message_id, int):
        return
    previous = await kv.get_redis().set(
        f"alert-msg:{customer.shop_id}:{service_id}", str(message_id), ex=ALERT_MESSAGE_TTL, get=True
    )
    if isinstance(previous, bytes):
        previous = previous.decode()
    if previous is None or not str(previous).isdigit() or int(previous) == message_id:
        return
    bot = await telegram.bot_for(customer.shop_id)
    if bot is None:
        return
    try:
        await bot.delete_message(customer.telegram_id, int(previous))
    except Exception:  # already gone, too old, or chat closed: nothing to clean up
        log.debug("stale alert %s for service %s was not deleted", previous, service_id)


async def service_alert(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.core.formatting import size

    service = await session.get(Service, int(job.payload["service_id"]))
    if service is None:
        return
    customer = await session.get(Customer, service.customer_id)
    if customer is None:
        return
    alert = str(job.payload["alert"])
    if alert == "deleted":  # the cleanup removed it: the «cron» report for the shop
        from paurubot.notifications import log_reports

        async def deleted_report() -> None:
            text = await log_reports.service_deleted(session, service, customer)
            markup = await log_reports.manage_markup(session, service.shop_id, customer)
            await report(session, service.shop_id, "services", text, markup=markup)

        await _quietly(deleted_report())
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
    message = await _to_customer(session, customer, text, reply_markup=markup)
    if message is not None and alert in REPLACEABLE_ALERTS:
        await _replace_alert(customer, service.id, message)


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


async def _referral_report(session: AsyncSession, referrer: Customer, payload: dict[str, Any]) -> None:
    """The commission for an invited customer's purchase, with the referrer's balance (GRW-01)."""
    from paurubot.ledger.accounts import wallet_balance
    from paurubot.notifications import log_reports

    buyer = await session.get(Customer, int(payload["buyer_id"])) if payload.get("buyer_id") else None
    if buyer is None:
        return
    order = await session.get(Order, int(payload["order_id"])) if payload.get("order_id") else None
    after = payload.get("balance_after")
    text = await log_reports.referral_paid(
        session,
        buyer=buyer,
        referrer=referrer,
        order=order,
        reward=int(payload["amount"]),
        percent=int(payload.get("percent") or 0),
        balance_after=int(after)
        if after is not None
        else await wallet_balance(session, referrer.shop_id, referrer.id),
    )
    await report(
        session,
        referrer.shop_id,
        "referrals",
        text,
        markup=await log_reports.manage_markup(session, referrer.shop_id, referrer),
    )


async def new_user_report(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.notifications import log_reports

    customer = await session.get(Customer, int(job.payload["customer_id"]))
    if customer is None:
        return
    text, manage = await log_reports.new_user(session, customer)
    await report(session, customer.shop_id, "users", text, markup=manage)


async def simple_customer(session: AsyncSession, job: queue.Claimed) -> None:
    customer = await session.get(Customer, int(job.payload["customer_id"]))
    if customer is None:
        return
    if job.kind == "private_code":
        from paurubot.core import marketing

        if not await marketing.allowed(session, customer):
            return
    t = await _texts(session, customer)
    if job.kind == "referral_joined":
        text = t("referral_joined", name=esc(str(job.payload.get("name", ""))))
    elif job.kind == "referral_reward":
        text = t("referral_reward", amount=money(int(job.payload["amount"])))
        await _quietly(_referral_report(session, customer, job.payload))
    elif job.kind == "referral_config_ready":
        text = t("referral_config_ready", available=int(job.payload.get("available") or 1))
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
        event="reseller_application",
    )


async def _ticket_staff_chats(session: AsyncSession, ticket: Ticket) -> list[int]:
    """An assigned ticket goes to its assignee only; otherwise to everyone who answers tickets (F-14)."""
    chats = await staff_chats(session, ticket.shop_id, "tickets.reply")
    if ticket.assigned_staff_id:
        assignee = await session.get(Staff, ticket.assigned_staff_id)
        if (
            assignee is not None
            and assignee.is_active
            and assignee.telegram_id
            and not rbac.is_scoped(assignee.role)
            and rbac.allowed(assignee.role, "tickets.reply")
        ):
            return [assignee.telegram_id]
    return chats


async def _report_ticket(
    session: AsyncSession, ticket: Ticket, department: TicketDepartment | None, text: str
) -> None:
    """A department with its own chat gets its tickets there; the rest go to the "tickets" log topic."""
    if department is not None and department.log_chat_id:
        try:
            await telegram.send_text(
                department.log_chat_id,
                text,
                message_thread_id=department.log_thread_id,
                shop_id=ticket.shop_id,
            )
        except telegram.Undeliverable:
            log.warning("ticket department %s chat is unreachable", department.id)
        return
    await report(session, ticket.shop_id, "tickets", text)


TICKET_LIVE_SECONDS = 8
TICKET_FRESH_SECONDS = 120


async def _ticket_service(session: AsyncSession, ticket: Ticket) -> dict[str, Any] | None:
    """The ticket's service as the panel shows it now (REF-09), saved on the ticket.

    Read under the service lease like every other panel read (SEC-F09) and within a few seconds,
    in this background job so the customer never waits. A busy service or an unreachable panel
    keeps the last synced numbers, marked as such; a reading under two minutes old is reused.
    """
    from datetime import datetime

    from paurubot import kv
    from paurubot.core import ticket_media
    from paurubot.panels import PanelError
    from paurubot.provisioning import lifecycle, sync

    if ticket.service_id is None:
        return ticket.service_context
    context = ticket.service_context or {}
    synced = context.get("synced_at")
    if context.get("checked") == "live" and isinstance(synced, str):
        if (utcnow() - datetime.fromisoformat(synced)).total_seconds() < TICKET_FRESH_SECONDS:
            return context
    ticket_id, service_id = ticket.id, ticket.service_id  # a rollback below expires both objects
    service = await session.get(Service, service_id)
    if service is None or service.shop_id != ticket.shop_id or service.customer_id != ticket.customer_id:
        return ticket.service_context
    checked, down = "stored", False
    if service.status != ServiceStatus.DELETED:
        try:
            async with kv.lease(lifecycle.service_lease(service.id), ttl=sync.BACKGROUND_LEASE_TTL):
                previous = service.status
                async with asyncio.timeout(TICKET_LIVE_SECONDS):
                    await lifecycle.refresh(session, service)
                if service.status != ServiceStatus.DELETED:
                    await sync.evaluate_alerts(session, service, previous)  # never skip an alert
                await session.commit()
                checked = "live"
        except kv.LockNotAcquired:
            pass  # being changed right now: the stored numbers will do
        except (PanelError, TimeoutError):
            await session.rollback()
            down = True
        ticket = await session.get(Ticket, ticket_id, populate_existing=True) or ticket
        service = await session.get(Service, service_id, populate_existing=True) or service
    ticket.service_context = ticket_media.service_context(service, checked=checked, panel_down=down)
    await session.commit()
    return ticket.service_context


def ticket_service_line(t: Texts, context: dict[str, Any]) -> str:
    """One line for staff: status, usage, end date and where the numbers come from (REF-09)."""
    from datetime import datetime

    from paurubot.core.formatting import size

    limit = int(context.get("data_limit") or 0)
    used = int(context.get("used_bytes") or 0)
    expire = context.get("expire_at")
    line = t(
        "adm_ticket_service",
        id=context.get("id"),
        status=t(f"status_{context.get('status')}"),
        used=size(used),
        limit=size(limit) if limit else t("unlimited"),
        expires=jdate(datetime.fromisoformat(expire)) if isinstance(expire, str) else t("unlimited"),
    )
    if context.get("panel_down"):
        return line + "\n" + t("adm_ticket_service_down")
    if context.get("checked") != "live":
        return line + "\n" + t("adm_ticket_service_stored")
    return line


async def ticket_new_message(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.bot.callbacks import Admin
    from paurubot.core.tickets import PRIORITY_ICON

    ticket = await session.get(Ticket, int(job.payload["ticket_id"]))
    if ticket is None:
        return
    shop = await session.get(Shop, ticket.shop_id)
    customer = await session.get(Customer, ticket.customer_id)
    if shop is None or customer is None:
        return
    t = await texts_for(session, shop.id, shop.default_locale)
    department = await session.get(TicketDepartment, ticket.department_id) if ticket.department_id else None
    text = t("adm_ticket_new", id=ticket.id, subject=esc(ticket.subject), customer=esc(customer.display_name))
    details = [f"{PRIORITY_ICON.get(ticket.priority, '')} {t(f'ticket_priority_{ticket.priority}')}"]
    if department is not None:
        details.insert(0, t("adm_ticket_department", department=esc(department.title)))
    text += "\n" + " · ".join(details)
    # _ticket_service may roll back, which expires the loaded rows: keep their ids, reload after.
    shop_id, department_id = shop.id, department.id if department is not None else None
    context = await _ticket_service(session, ticket)
    if context:
        text += "\n" + ticket_service_line(t, context)
    if department_id is not None:
        department = await session.get(TicketDepartment, department_id, populate_existing=True)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_open_ticket"), callback_data=Admin(section="ticket", id=ticket.id).pack()
                )
            ]
        ]
    )
    for chat_id in await _ticket_staff_chats(session, ticket):
        try:
            await telegram.send_text(chat_id, text, reply_markup=markup, shop_id=shop_id)
        except telegram.Undeliverable:
            continue
    await _report_ticket(session, ticket, department, text)


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
    shop = await session.get(Shop, job.shop_id) if job.shop_id is not None else None
    error = esc(redact(str(job.payload.get("error") or "-")))
    if shop is not None:
        t = await texts_for(session, shop.id, shop.default_locale)
        text = t(
            "report_job_dead",
            id=job.payload.get("job_id"),
            queue=esc(str(job.payload.get("queue"))),
            kind=esc(str(job.payload.get("kind"))),
            error=error,
        )
    else:
        text = (
            "⚠️ <b>Background job exhausted retries</b>"
            f"\n#{esc(str(job.payload.get('job_id')))}"
            f" · {esc(str(job.payload.get('queue')))} / {esc(str(job.payload.get('kind')))}"
            f"\n{error}"
        )
    await report_error(session, job.shop_id, text)


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
    await _to_staff(session, shop.id, "panels.read", text, event="panel")
    await report(session, shop.id, "panels", text)


async def reseller_notice(session: AsyncSession, job: queue.Claimed) -> None:
    """Suspension, resumption and low-balance notices to a reseller, copied to the owners (TEN-06)."""
    from paurubot.core.resellers import runway_line
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
    text = t(f"reseller_{notice}", **values)
    days = job.payload.get("days") if notice == "low_balance" else None
    if days is not None:  # F-21: how long the wallet lasts at the current pace
        text += "\n" + runway_line(t, float(days))
    await _to_customer(session, customer, text)
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
    """Order reports in the shop's public channels, with the buyer id masked (GRW-08)."""
    from paurubot import kv
    from paurubot.core import channels, growth
    from paurubot.db.models import Plan, TrustChannel

    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None or order.kind.value not in channels.TRUST_KINDS:
        return
    if order.amount <= 0 and order.kind != OrderKind.TRIAL:
        return  # a free extra or a move is not worth a public post
    # Every active channel that lists this order kind gets the report; a shop may keep several.
    targets = [
        row
        for row in await session.scalars(
            select(TrustChannel)
            .where(TrustChannel.shop_id == order.shop_id, TrustChannel.is_active.is_(True))
            .order_by(TrustChannel.sort, TrustChannel.id)
        )
        if order.kind.value in (row.kinds or [])
    ]
    if not targets:
        return
    customer = await session.get(Customer, order.customer_id)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    service = await session.get(Service, order.service_id) if order.service_id else None
    shop = await session.get(Shop, order.shop_id)
    t = await texts_for(
        session,
        order.shop_id,
        shop.default_locale if shop else None,
        {"shop": esc(shop.title if shop else "")},
    )
    when = order.completed_at or order.paid_at or order.created_at or utcnow()
    # The panel username can contain the buyer's id, so the public channel only ever sees the
    # service's own label (or the plan name when it has none).
    service_name = (service.label if service else None) or (plan.title if plan else None)
    fields: dict[str, Any] = {
        "buyer": growth.masked_id(customer.telegram_id if customer else None),
        "name": esc(growth.masked(customer.first_name if customer else None)),
        "plan": esc(plan.title) if plan else "—",
        "category": esc(plan.category.title) if plan and plan.category else "—",
        "service": esc(service_name) if service_name else "—",
        "amount": money(order.amount),
        "date": jdate(when),
        "time": local(when).strftime("%H:%M:%S"),
    }
    special = {
        OrderKind.PURCHASE: "trust_purchase",
        OrderKind.RENEW: "trust_renew",
        OrderKind.TRIAL: "trust_trial",
    }
    key = special.get(order.kind)
    text = t(key, **fields) if key else t("trust_generic", kind=t(f"order_kind_{order.kind.value}"), **fields)
    markup: InlineKeyboardMarkup | None = None
    if shop and shop.bot_username:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("btn_trust_buy"), url=f"https://t.me/{shop.bot_username}")]
            ]
        )
    for row in targets:
        try:
            await telegram.send_text(row.chat_id, text, shop_id=order.shop_id, reply_markup=markup)
        except telegram.Undeliverable as exc:
            log.warning("trust channel post failed for shop %s in %s", order.shop_id, row.chat_id)
            # The admin panel shows the channel as failing instead of dropping posts silently.
            await kv.mark(f"trust:{order.shop_id}:{row.chat_id}", TRUST_FAIL_TTL, str(exc) or "1")
        else:
            await kv.unmark(f"trust:{order.shop_id}:{row.chat_id}")


async def lottery_result(session: AsyncSession, job: queue.Claimed) -> None:
    """Tell each winner what they won and staff who won what (GRW-04). A new service prize is
    a free order: the config itself reaches the winner like any purchase."""
    from paurubot import kv
    from paurubot.db.models import Lottery
    from paurubot.notifications import log_reports

    lottery = await session.get(Lottery, int(job.payload["lottery_id"]))
    if lottery is None:
        return
    # A retry after a failed send must not tell anyone twice: each message is marked once sent.
    for award in log_reports.lottery_awards(lottery):
        sent = f"lottery:{lottery.id}:told:{award['rank']}"
        customer = await session.get(Customer, int(award["customer_id"]))
        if customer is None or await kv.marked([sent]):
            continue
        t = await _texts(session, customer)
        tail = {"paid": "lottery_won_paid", "queued": "lottery_won_service"}.get(
            str(award.get("status")), "lottery_won_manual"
        )
        await _to_customer(session, customer, f"{_won_head(t, lottery, award)}\n{t(tail)}")
        await kv.mark(sent, LOTTERY_SENT_TTL)
    staff_sent = f"lottery:{lottery.id}:told:staff"
    if await kv.marked([staff_sent]):
        return
    for text, markup in await log_reports.lottery_result(session, lottery):
        await _to_staff(session, lottery.shop_id, "broadcast.send", text, markup=markup, event="lottery")
        await report(session, lottery.shop_id, "reports", text, markup=markup)
    await kv.mark(staff_sent, LOTTERY_SENT_TTL)


LOTTERY_SENT_TTL = 30 * 86400


def _won_head(t: Texts, lottery: Any, award: dict[str, Any]) -> str:
    from paurubot.notifications import log_reports

    return t(
        "lottery_won_head",
        title=esc(lottery.title),
        rank=t("lot_rank", rank=award["rank"]),
        prize=log_reports.prize_text(t, award),
    )


async def stock_delivered(session: AsyncSession, job: queue.Claimed) -> None:
    """Send the customer the ready-made config they bought (CAT-05)."""
    from paurubot.core import stock

    order = await session.get(Order, int(job.payload["order_id"]))
    if order is None or not order.params.get("stock_item_id"):
        return
    customer = await session.get(Customer, order.customer_id)
    item = await session.get(StockItem, int(order.params["stock_item_id"]))
    if customer is None or item is None:
        return
    t = await _texts(session, customer)
    if item.customer_id != customer.id or item.order_id != order.id or item.status != stock.SOLD:
        return
    from paurubot.notifications import manual_delivery

    try:
        await manual_delivery.send(customer, item, t("stock_ready"))
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
                from paurubot.core import manual_stock
                from paurubot.notifications import manual_delivery

                if any(p.data is not None or p.as_file for p in manual_stock.parts(item)):
                    try:
                        await manual_delivery.send(customer, item, t("stock_ready"))
                    except telegram.Undeliverable:
                        pass
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
            "error_report": error_reports.deliver,
            "order_completed": order_completed,
            "account_ready": account_ready,
            "account_alert": account_alert,
            "own_panel_alert": own_panel_alert,
            "account_changed": account_changed,
            "account_auto_renew": account_auto_renew,
            "order_failed": order_failed,
            "payment_fulfilled": payment_fulfilled,
            "payment_rejected": payment_rejected,
            "payment_voided": payment_voided,
            "card_sms": card_sms,
            "stars_unmatched": stars_unmatched,
            "receipt_review": receipt_review,
            "review_decided": review_decided,
            "payment_fix_requested": payment_fix_requested,
            "service_alert": service_alert,
            "auto_renew_notice": auto_renew_notice,
            "referral_joined": simple_customer,
            "referral_reward": simple_customer,
            "referral_config_ready": simple_customer,
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
            "new_user_report": new_user_report,
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


def broadcast_copier(bot: Any, row: Broadcast) -> Callable[[int], Callable[[], Awaitable[Any]]]:
    """How one broadcast reaches one chat; also used for the staff test send (F-16)."""
    markup = None
    if row.buttons:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=b["text"], url=b["url"])]
                for b in row.buttons
                if b.get("text") and b.get("url")
            ]
        )
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
    return copier


async def run_broadcast(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.core import broadcasts

    row = await session.get(Broadcast, int(job.payload["broadcast_id"]))
    run = int(job.payload.get("run") or 0)
    if row is None or broadcasts.should_stop(row, run):
        return  # finished, canceled, paused, or a newer run took over (F-16)
    bot = await telegram.bot_for(row.shop_id)
    if bot is None:
        raise Permanent("this shop's bot is stopped")
    copier = broadcast_copier(bot, row)
    pin = bool((row.audience or {}).get("pin"))
    pin_hours = int((row.audience or {}).get("pin_hours") or 0)
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
        await session.refresh(row, ["status", "run"])
        if broadcasts.should_stop(row, run):
            return
        for customer in batch:
            # Re-evaluate the full campaign audience immediately before each delivery. Scalar
            # reads bypass the identity map, including opt-outs committed after batch loading.
            eligible = await session.scalar(
                select(Customer.id).where(Customer.id == customer.id, *_audience(row))
            )
            if eligible is None:
                row.skipped = (row.skipped or 0) + 1
                row.cursor = customer.id
                continue
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
            except telegram.Undeliverable as exc:
                customer.blocked_bot_at = utcnow()
                row.failed += 1
                if broadcasts.undeliverable_reason(exc) == "blocked":
                    row.blocked = (row.blocked or 0) + 1
                else:
                    row.deleted = (row.deleted or 0) + 1
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
    # Do not put this in audience_conditions: wallet gifts are ledger operations, not ads.
    conditions.append(Customer.marketing_opt_out.is_(False))
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
