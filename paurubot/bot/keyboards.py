from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from paurubot.bot.callbacks import Lang, Nav, Pay, Shop, Svc, Ticket, Wallet
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
    "btn_apps",
    "btn_language",
    "btn_miniapp",
    "btn_admin",
)


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


def single(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]]
    )


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
        style = plan.button_style if isinstance(plan.button_style, dict) else {}
        extra: dict[str, Any] = {}
        if style.get("style") in {"primary", "success", "danger"}:
            extra["style"] = style["style"]
        if style.get("emoji_id"):
            extra["icon_custom_emoji_id"] = str(style["emoji_id"])
        kb.row(
            InlineKeyboardButton(
                text=t("plan_button", title=plan_title(t, plan), price=money(price)),
                callback_data=Shop(step="plan", plan=plan.id, svc=svc, kind=kind).pack(),
                **extra,
            )
        )
    if custom:
        kb.row(InlineKeyboardButton(text=t("btn_custom_plan"), callback_data=Shop(step="custom").pack()))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back_cb))
    return kb.as_markup()


def checkout_kb(
    t: Texts, *, order_id: int, can_wallet: bool, methods: Sequence[str], discount: bool, bulk_plan: int = 0
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
    if bulk_plan:
        kb.row(
            InlineKeyboardButton(
                text=t("btn_bulk_buy"), callback_data=Shop(step="bulk", plan=bulk_plan).pack()
            )
        )
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


def wallet_kb(t: Texts, *, transfer: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_topup"), callback_data=Wallet(action="topup"))
    kb.button(text=t("btn_history"), callback_data=Wallet(action="history"))
    kb.button(text=t("btn_redeem_gift"), callback_data=Wallet(action="gift"))
    if transfer:
        kb.button(text=t("btn_transfer"), callback_data=Wallet(action="transfer"))
    kb.button(text=t("btn_my_codes"), callback_data=Wallet(action="codes"))
    kb.adjust(2, 2, 1)
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


def services_kb(
    t: Texts,
    items: Sequence[Service],
    page: int,
    has_next: bool,
    *,
    stock: bool = False,
    muted: bool | None = None,
    all_links: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if all_links:
        kb.row(InlineKeyboardButton(text=t("btn_all_links"), callback_data=Svc(action="all").pack()))
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
    if stock:
        kb.row(InlineKeyboardButton(text=t("btn_stock"), callback_data=Svc(action="stock").pack()))
    if muted is not None:
        kb.row(
            InlineKeyboardButton(
                text=t("btn_alerts_on") if muted else t("btn_alerts_off"),
                callback_data=Svc(action="mute").pack(),
            )
        )
    return kb.as_markup()


def back_to_services(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Svc(action="list").pack()))
    return kb.as_markup()


def service_kb(
    t: Texts,
    service: Service,
    *,
    extras: tuple[int | None, int | None],
    upgrade: bool,
    move: bool = False,
    card: bool = False,
    cancel: bool = False,
    link: str | None = None,
    auto: bool | None = None,
) -> InlineKeyboardMarkup:
    from paurubot.core.orders import can_change

    sid = service.id
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_refresh"), callback_data=Svc(action="view", id=sid))
    kb.button(text=t("btn_links"), callback_data=Svc(action="links", id=sid))
    kb.button(text=t("btn_qr"), callback_data=Svc(action="qr", id=sid))
    if card:
        kb.button(text=t("btn_infocard"), callback_data=Svc(action="card", id=sid))
    if (
        not service.is_trial
        and can_change(service, "renew")
        and (service.panel is None or (service.panel.options or {}).get("allow_renewals") is not False)
    ):
        kb.button(text=t("btn_renew"), callback_data=Svc(action="renew", id=sid))
    if not service.is_trial:
        if extras[0] is not None:
            kb.button(text=t("btn_extra_volume"), callback_data=Svc(action="extra_gb", id=sid))
        if extras[1] is not None:
            kb.button(text=t("btn_extra_days"), callback_data=Svc(action="extra_days", id=sid))
    if auto is not None:
        kb.button(
            text=t("btn_auto_renew_on") if auto else t("btn_auto_renew_off"),
            callback_data=Svc(action="auto", id=sid),
        )
    if upgrade and can_change(service, "upgrade"):
        kb.button(text=t("btn_upgrade"), callback_data=Svc(action="upgrade", id=sid))
    if move and not service.is_trial:
        kb.button(text=t("btn_move"), callback_data=Svc(action="move", id=sid))
    if can_change(service, "revoke"):
        kb.button(text=t("btn_revoke"), callback_data=Svc(action="revoke", id=sid))
    kb.button(text=t("btn_rename"), callback_data=Svc(action="rename", id=sid))
    kb.button(text=t("btn_report_problem"), callback_data=Svc(action="report", id=sid))
    if cancel and not service.is_trial:
        kb.button(text=t("btn_cancel_service"), callback_data=Svc(action="cancel", id=sid))
    kb.adjust(2)
    if link:
        from aiogram.types import CopyTextButton

        kb.row(InlineKeyboardButton(text=t("btn_copy_link"), copy_text=CopyTextButton(text=link[:256])))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Svc(action="list").pack()))
    return kb.as_markup()


def auto_renew_kb(
    t: Texts,
    service_id: int,
    plans: Sequence[tuple[Plan, int]],
    *,
    gb_options: Sequence[int],
    day_options: Sequence[int],
    chosen: dict[str, Any] | None,
    removable: bool = False,
) -> InlineKeyboardMarkup:
    """Choosing what auto-renewal buys (F-02); the current choice carries a check mark."""
    chosen = chosen or {}
    mode = chosen.get("mode")

    def mark(on: bool, text: str) -> str:
        return f"✅ {text}" if on else text

    kb = InlineKeyboardBuilder()
    for plan, price in plans:
        on = mode == "renew" and chosen.get("plan_id") == plan.id
        kb.row(
            InlineKeyboardButton(
                text=mark(on, t("auto_renew_plan_option", plan=plan.title, price=money(price))),
                callback_data=Svc(action="auto_plan", id=service_id, value=plan.id).pack(),
            )
        )
    for kind, options, action in (("gb", gb_options, "auto_gb"), ("days", day_options, "auto_days")):
        if not options:
            continue
        current = chosen.get(kind) if mode == "extra" else None
        kb.row(
            *[
                InlineKeyboardButton(
                    text=mark(current == value, t(f"auto_renew_{kind}_option", value=value)),
                    callback_data=Svc(
                        action=action, id=service_id, value=0 if current == value else value
                    ).pack(),
                )
                for value in options
            ]
        )
    if mode or removable:
        kb.row(
            InlineKeyboardButton(
                text=t("btn_auto_renew_disable"), callback_data=Svc(action="auto_off", id=service_id).pack()
            )
        )
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Svc(action="view", id=service_id).pack()))
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


def move_kb(t: Texts, service_id: int, targets: Sequence[Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for location in targets:
        title = f"{location.flag} {location.title}" if location.flag else location.title
        kb.button(text=title, callback_data=Svc(action="move_to", id=service_id, value=location.id))
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


def copy_kb(items: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    """One-tap copy buttons (Bot API copy_text): (label, value) pairs, empty values skipped."""
    from aiogram.types import CopyTextButton

    rows = [
        [InlineKeyboardButton(text=label[:64], copy_text=CopyTextButton(text=str(value)[:256]))]
        for label, value in items
        if value
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
