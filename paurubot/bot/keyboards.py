from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from paurubot.bot.callbacks import Lang, Nav, Pay, Shop, Svc, Ticket, Wallet
from paurubot.config import get_settings
from paurubot.core.formatting import money
from paurubot.db.models import Category, ForcedChannel, Location, Plan, Service, ServiceStatus
from paurubot.i18n import Texts

STATUS_ICON = {
    ServiceStatus.PENDING: "⏳",
    ServiceStatus.ACTIVE: "🟢",
    ServiceStatus.ON_HOLD: "🟡",
    ServiceStatus.LIMITED: "🟠",
    ServiceStatus.EXPIRED: "🔴",
    ServiceStatus.DISABLED: "⚪️",
    ServiceStatus.DELETED: "⚫️",
}

MENU_KEYS = (
    "btn_buy",
    "btn_services",
    "btn_wallet",
    "btn_trial",
    "btn_referral",
    "btn_reseller",
    "btn_tickets",
    "btn_guide",
    "btn_language",
    "btn_admin",
)


def main_menu(t: Texts, *, is_staff: bool, trial: bool, referral: bool) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    url = get_settings().public_url
    if url.startswith("https://"):
        rows.append([KeyboardButton(text=t("btn_miniapp"), web_app=WebAppInfo(url=f"{url}/app/"))])
    rows.append([KeyboardButton(text=t("btn_buy")), KeyboardButton(text=t("btn_services"))])
    second = [KeyboardButton(text=t("btn_wallet"))]
    if trial:
        second.append(KeyboardButton(text=t("btn_trial")))
    rows.append(second)
    third = [KeyboardButton(text=t("btn_reseller"))]
    if referral:
        third.insert(0, KeyboardButton(text=t("btn_referral")))
    rows.append(third)
    rows.append([KeyboardButton(text=t("btn_tickets")), KeyboardButton(text=t("btn_guide"))])
    rows.append([KeyboardButton(text=t("btn_language"))])
    if is_staff:
        rows.append([KeyboardButton(text=t("btn_admin"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def cancel(t: Texts) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t("btn_cancel"))]], resize_keyboard=True)


def share_phone(t: Texts) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("btn_share_phone"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def join(t: Texts, channels: Sequence[ForcedChannel]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for channel in channels:
        kb.row(InlineKeyboardButton(text=channel.title, url=channel.url))
    kb.row(InlineKeyboardButton(text=t("btn_joined"), callback_data=Nav(to="joined").pack()))
    return kb.as_markup()


def terms(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_accept_terms"), callback_data=Nav(to="terms").pack())]
        ]
    )


def languages(t: Texts, enabled: Sequence[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code in enabled:
        kb.button(text=t(f"lang_{code}"), callback_data=Lang(code=code))
    kb.adjust(2)
    return kb.as_markup()


def url_button(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def back(t: Texts, callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("btn_back"), callback_data=callback)]]
    )


def locations_kb(t: Texts, items: Sequence[Location]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for loc in items:
        label = f"{loc.flag} {loc.title}" if loc.flag else loc.title
        kb.button(text=label, callback_data=Shop(step="loc", loc=loc.id))
    kb.adjust(2)
    return kb.as_markup()


def categories_kb(
    t: Texts, items: Sequence[Category], loc: int, other: bool, custom: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in items:
        kb.button(text=cat.title, callback_data=Shop(step="cat", loc=loc, cat=cat.id))
    if other:
        kb.button(text=t("btn_other_plans"), callback_data=Shop(step="cat", loc=loc, cat=0))
    kb.adjust(2)
    if custom:
        kb.row(InlineKeyboardButton(text=t("btn_custom_plan"), callback_data=Shop(step="custom").pack()))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Shop(step="start").pack()))
    return kb.as_markup()


def plan_title(t: Texts, plan: Plan) -> str:
    """Plan name with its own emoji and badge (CAT-02)."""
    style = plan.button_style if isinstance(plan.button_style, dict) else {}
    title = plan.title
    if style.get("emoji"):
        title = f"{style['emoji']} {title}"
    if style.get("badge"):
        title = f"{title} {t('badge_' + str(style['badge']))}"
    return title


def plans_kb(
    t: Texts,
    items: Sequence[tuple[Plan, int]],
    *,
    back_cb: str,
    svc: int = 0,
    kind: str = "",
    custom: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan, price in items:
        kb.row(
            InlineKeyboardButton(
                text=t("plan_button", title=plan_title(t, plan), price=money(price)),
                callback_data=Shop(step="plan", plan=plan.id, svc=svc, kind=kind).pack(),
            )
        )
    if custom:
        kb.row(InlineKeyboardButton(text=t("btn_custom_plan"), callback_data=Shop(step="custom").pack()))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back_cb))
    return kb.as_markup()


def checkout_kb(
    t: Texts, *, order_id: int, can_wallet: bool, methods: Sequence[str], discount: bool
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_wallet:
        kb.row(
            InlineKeyboardButton(
                text=t("btn_pay_wallet"), callback_data=Pay(method="wallet", order=order_id).pack()
            )
        )
    for method in methods:
        kb.row(
            InlineKeyboardButton(
                text=t(f"btn_pay_{method}"), callback_data=Pay(method=method, order=order_id).pack()
            )
        )
    if discount:
        kb.row(InlineKeyboardButton(text=t("btn_discount"), callback_data=Shop(step="discount").pack()))
    kb.row(InlineKeyboardButton(text=t("btn_cancel"), callback_data=Shop(step="cancel").pack()))
    return kb.as_markup()


def topup_methods_kb(t: Texts, amount: int, methods: Sequence[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for method in methods:
        kb.row(
            InlineKeyboardButton(
                text=t(f"btn_pay_{method}"), callback_data=Pay(method=method, amount=amount).pack()
            )
        )
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Wallet(action="home").pack()))
    return kb.as_markup()


def wallet_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_topup"), callback_data=Wallet(action="topup"))
    kb.button(text=t("btn_history"), callback_data=Wallet(action="history"))
    kb.button(text=t("btn_redeem_gift"), callback_data=Wallet(action="gift"))
    kb.adjust(2, 1)
    return kb.as_markup()


def amounts_kb(t: Texts, presets: Sequence[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in presets:
        kb.button(
            text=t("amount_button", amount=money(amount)),
            callback_data=Wallet(action="amount", amount=amount),
        )
    kb.button(text=t("btn_custom_amount"), callback_data=Wallet(action="custom"))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Wallet(action="home").pack()))
    return kb.as_markup()


def history_kb(t: Texts, before: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if before:
        kb.button(text=t("btn_more"), callback_data=Wallet(action="history", before=before))
    kb.button(text=t("btn_back"), callback_data=Wallet(action="home"))
    kb.adjust(1)
    return kb.as_markup()


def services_kb(t: Texts, items: Sequence[Service], page: int, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for service in items:
        name = service.label or service.panel_username
        kb.row(
            InlineKeyboardButton(
                text=f"{STATUS_ICON.get(service.status, '')} {name}",
                callback_data=Svc(action="view", id=service.id).pack(),
            )
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text=t("btn_prev"), callback_data=Svc(action="list", page=page - 1).pack())
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(text=t("btn_next"), callback_data=Svc(action="list", page=page + 1).pack())
        )
    if nav:
        kb.row(*nav)
    return kb.as_markup()


def service_kb(
    t: Texts, service: Service, *, extras: tuple[int | None, int | None], upgrade: bool
) -> InlineKeyboardMarkup:
    sid = service.id
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_refresh"), callback_data=Svc(action="view", id=sid))
    kb.button(text=t("btn_links"), callback_data=Svc(action="links", id=sid))
    kb.button(text=t("btn_qr"), callback_data=Svc(action="qr", id=sid))
    if not service.is_trial:
        kb.button(text=t("btn_renew"), callback_data=Svc(action="renew", id=sid))
        if extras[0] is not None:
            kb.button(text=t("btn_extra_volume"), callback_data=Svc(action="extra_gb", id=sid))
        if extras[1] is not None:
            kb.button(text=t("btn_extra_days"), callback_data=Svc(action="extra_days", id=sid))
    if upgrade:
        kb.button(text=t("btn_upgrade"), callback_data=Svc(action="upgrade", id=sid))
    kb.button(text=t("btn_revoke"), callback_data=Svc(action="revoke", id=sid))
    kb.button(text=t("btn_rename"), callback_data=Svc(action="rename", id=sid))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Svc(action="list").pack()))
    return kb.as_markup()


def extra_kb(
    t: Texts, service_id: int, kind: str, options: Sequence[int], unit_price: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value in options:
        kb.button(
            text=t(f"extra_{kind}_option", value=value, price=money(value * unit_price)),
            callback_data=Svc(action=f"buy_{kind}", id=service_id, value=value),
        )
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Svc(action="view", id=service_id).pack()))
    return kb.as_markup()


def confirm(t: Texts, yes: str, no: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_yes"), callback_data=yes)
    kb.button(text=t("btn_no"), callback_data=no)
    return kb.as_markup()


def tickets_kb(t: Texts, items: Sequence[Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ticket in items:
        kb.row(
            InlineKeyboardButton(
                text=t(
                    "ticket_button",
                    id=ticket.id,
                    subject=ticket.subject,
                    status=t(f"ticket_status_{ticket.status.value}"),
                ),
                callback_data=Ticket(action="view", id=ticket.id).pack(),
            )
        )
    kb.row(InlineKeyboardButton(text=t("btn_new_ticket"), callback_data=Ticket(action="new").pack()))
    return kb.as_markup()


def ticket_kb(t: Texts, ticket_id: int, closed: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not closed:
        kb.button(text=t("btn_ticket_reply"), callback_data=Ticket(action="reply", id=ticket_id))
        kb.button(text=t("btn_ticket_close"), callback_data=Ticket(action="close", id=ticket_id))
    kb.button(text=t("btn_back"), callback_data=Ticket(action="list"))
    kb.adjust(2)
    return kb.as_markup()
