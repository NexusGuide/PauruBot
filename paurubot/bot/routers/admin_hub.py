"""Admin hub in the bot (ADM-12): feature switches by category, filtered customer lists and
sales stats for a chosen period, the in-bot management screens other VPN shop bots offer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from paurubot.bot.callbacks import Admin, Nav
from paurubot.bot.context import StaffIdentity
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerAccountKind,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    PaymentStatus,
    ResellerProfile,
    Service,
    ServiceStatus,
    VerifiedCard,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="admin_hub")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

PAGE = 10
TZ = ZoneInfo("Asia/Tehran")

# Feature switches grouped like the "feature status" screens; every key is a bool in SPECS.
# Payment gateways are not here: the finance hub owns them with readiness checks and review.
FEATURES: dict[str, list[str]] = {
    "main": [
        "bot_enabled",
        "terms_required",
        "require_phone",
        "trial_requires_phone",
        "card_verify_enabled",
        "miniapp_enabled",
        "premium_emoji_enabled",
    ],
    "users": [
        "reseller_applications_enabled",
        "reseller_web_access",
        "reseller_bot_open",
        "panel_accounts_enabled",
        "ip_notice_enabled",
        "ban_disables_services",
        "self_delete_enabled",
    ],
    "sales": [
        "bulk_buy_enabled",
        "custom_buy_enabled",
        "custom_username_enabled",
        "extras_enabled",
        "upgrade_enabled",
        "move_enabled",
        "renew_resets_usage",
        "renew_queue_when_active",
        "info_card_enabled",
        "info_card_qr",
        "branded_sub_links",
    ],
    "growth": [
        "trial_enabled",
        "referral_enabled",
        "referral_every_purchase",
        "start_gift_referred_only",
        "wheel_enabled",
        "wheel_buyers_only",
        "lottery_enabled",
        "loyalty_enabled",
    ],
}
FEATURE_KEYS: list[str] = [key for keys in FEATURES.values() for key in keys]

# A gateway cannot be switched on before its credentials are set (Shop setup screen or web panel).
REQUIRES: dict[str, tuple[str, ...]] = {
    "zarinpal_enabled": ("zarinpal_merchant",),
    "nowpayments_enabled": ("nowpayments_api_key",),
    "plisio_enabled": ("plisio_api_key",),
    "aqayepardakht_enabled": ("aqayepardakht_pin",),
    "tetra98_enabled": ("tetra98_api_key",),
    "variza_enabled": ("variza_api_token",),
    "abangateway_enabled": ("abangateway_api_key",),
    "blupal_enabled": ("blupal_api_key",),
    "tronado_enabled": ("tronado_api_key",),
    "cubepay_enabled": ("cubepay_api_key",),
    "atlaspay_enabled": ("atlaspay_api_key",),
}
ANY_OF: dict[str, tuple[str, ...]] = {"crypto_enabled": ("tron_address", "ton_address")}

USER_FILTERS = ("all", "balance", "negative", "referrals", "active", "resellers", "banned", "cards")
PERIODS = ("today", "hour", "yesterday", "week", "month", "all")


def hub_buttons(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    out: list[tuple[str, Admin]] = []
    if staff.can("settings.write"):
        out.append((t("adm_hub_features"), Admin(section="feat")))
    if staff.can("customers.read"):
        out.append((t("adm_hub_users"), Admin(section="ulist")))
    if staff.can("reports.read"):
        out.append((t("adm_hub_stats"), Admin(section="pstats")))
    # Imported here: admin_config imports can_enable from this module.
    from paurubot.bot.routers.admin_config import config_buttons

    out += config_buttons(t, staff)
    return out


def can_enable(key: str, cfg: dict[str, Any]) -> bool:
    specs = shop_settings.SPECS
    if any(name in specs and not cfg.get(name) for name in REQUIRES.get(key, ())):
        return False
    choices = [name for name in ANY_OF.get(key, ()) if name in specs]
    return not choices or any(cfg.get(name) for name in choices)


def period_range(name: str, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Start (inclusive) and end (exclusive) in UTC; days follow Tehran time."""
    now = now or datetime.now(UTC)
    midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    if name == "hour":
        return now - timedelta(hours=1), None
    if name == "today":
        return midnight, None
    if name == "yesterday":
        return midnight - timedelta(days=1), midnight
    if name == "week":
        return now - timedelta(days=7), None
    if name == "month":
        return now - timedelta(days=30), None
    return None, None


def _between(column: Any, start: datetime | None, end: datetime | None) -> list[Any]:
    out: list[Any] = []
    if start is not None:
        out.append(column >= start)
    if end is not None:
        out.append(column < end)
    return out


def user_conditions(shop_id: int, name: str) -> list[Any]:
    conds: list[Any] = [Customer.shop_id == shop_id, Customer.telegram_id > 0]
    wallet = select(LedgerAccount.id).where(
        LedgerAccount.shop_id == shop_id,
        LedgerAccount.customer_id == Customer.id,
        LedgerAccount.kind == LedgerAccountKind.WALLET,
    )
    if name == "balance":
        conds.append(exists(wallet.where(LedgerAccount.balance > 0)))
    elif name == "negative":
        conds.append(exists(wallet.where(LedgerAccount.balance < 0)))
    elif name == "referrals":
        referee = aliased(Customer)
        conds.append(
            exists(select(referee.id).where(referee.shop_id == shop_id, referee.referrer_id == Customer.id))
        )
    elif name == "active":
        conds.append(
            exists(
                select(Service.id).where(
                    Service.shop_id == shop_id,
                    Service.customer_id == Customer.id,
                    Service.status == ServiceStatus.ACTIVE,
                )
            )
        )
    elif name == "resellers":
        conds.append(
            exists(
                select(ResellerProfile.id).where(
                    ResellerProfile.shop_id == shop_id,
                    ResellerProfile.customer_id == Customer.id,
                    ResellerProfile.is_active.is_(True),
                )
            )
        )
    elif name == "cards":
        conds.append(
            exists(
                select(VerifiedCard.id).where(
                    VerifiedCard.shop_id == shop_id,
                    VerifiedCard.customer_id == Customer.id,
                    VerifiedCard.status == "approved",
                )
            )
        )
    elif name == "banned":
        conds.append(Customer.is_banned.is_(True))
    return conds


def _back(t: Texts, target: Admin) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t("btn_back"), callback_data=target.pack())]


# --- feature switches ------------------------------------------------------------------------


def features_kb(t: Texts, cfg: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    """Feature categories; only things PauruBot really has, one per row."""
    from paurubot.bot.routers.admin_general import CRONS_PAGE

    cfg = cfg or {}
    rows = [
        [_btn(t(f"adm_feat_group_{group}"), Admin(section="feat", action="cat", page=index))]
        for index, group in enumerate(FEATURES)
    ]
    emojis = len(cfg.get("premium_emojis") or {})
    rows += [
        [_btn(t("adm_feat_group_payments"), Admin(section="fgw", action="show"))],
        [_btn(t("adm_cfg_group_crons"), Admin(section="cfg", action="grp", page=CRONS_PAGE))],
        [_btn(t("adm_feat_emoji", count=emojis), Admin(section="look", action=""))],
        [_btn(t("adm_feat_system"), Admin(section="status", action=""))],
        [
            _btn(t("adm_x_btn"), Admin(section="xcfg", action="")),
            _btn(t("adm_grw_btn"), Admin(section="grw", action="")),
        ],
        [
            _btn(t("adm_ud_admin"), Admin(section="home")),
            _btn(t("adm_ud_close"), Admin(section="gset", action="close")),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, target: Admin) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=target.pack())


# Feature -> (label, target, permission) of its own settings page, shown as a third cell.
SETTINGS_LINKS: dict[str, tuple[str, Admin, str]] = {
    "reseller_applications_enabled": (
        "adm_feat_link_requests",
        Admin(section="applications", action=""),
        "resellers.manage",
    ),
    "self_delete_enabled": (
        "adm_feat_link_settings",
        Admin(section="xcfg", action="grp", page=3),
        "settings.write",
    ),
    "bulk_buy_enabled": ("adm_feat_link_settings", Admin(section="sconf", action="show"), "settings.write"),
    "custom_buy_enabled": (
        "adm_feat_link_settings",
        Admin(section="xcfg", action="grp", page=2),
        "settings.write",
    ),
    "extras_enabled": ("adm_feat_link_prices", Admin(section="xcfg", action="grp", page=3), "settings.write"),
    "move_enabled": ("adm_feat_link_prices", Admin(section="xcfg", action="grp", page=3), "settings.write"),
    "trial_enabled": (
        "adm_feat_link_settings",
        Admin(section="settings", action="group", page=2),
        "settings.write",
    ),
    "referral_enabled": (
        "adm_feat_link_settings",
        Admin(section="xcfg", action="grp", page=1),
        "settings.write",
    ),
    "wheel_enabled": ("adm_feat_link_prizes", Admin(section="grw", action="prizes"), "settings.write"),
    "lottery_enabled": ("adm_feat_link_manage", Admin(section="grw", action="lots"), "settings.write"),
}
# Extra full-width rows after a group's switches: (label, target, permission).
GROUP_LINKS: dict[str, tuple[tuple[str, Admin, str], ...]] = {
    "users": (("adm_tickets", Admin(section="tickets", action=""), "tickets.reply"),),
    "growth": (("adm_grw_btn", Admin(section="grw", action=""), "settings.write"),),
}


def group_text(t: Texts, group: str) -> str:
    return t(f"adm_feat_text_{group}")


def group_kb(
    t: Texts, group: str, cfg: dict[str, Any], staff: StaffIdentity | None = None
) -> InlineKeyboardMarkup:
    """Status on the left, title on the right (both switch); a third cell opens settings."""

    def can(permission: str) -> bool:
        return staff.can(permission) if staff is not None else permission == "settings.write"

    noop = Admin(section="gset", action="noop")
    rows = []
    if group == "main":
        rows.append([_btn(t("adm_feat_col_status"), noop), _btn(t("adm_feat_col_title"), noop)])
    origin = list(FEATURES).index(group) + 1
    for key in FEATURES[group]:
        on = bool(cfg.get(key))
        # page carries the wanted value (1 = off, 2 = on); a double tap cannot undo itself.
        target = Admin(section="feat", action="tog", id=FEATURE_KEYS.index(key), page=1 if on else 2)
        status = t("adm_gs_on") if on else t("adm_gs_off")
        row = [_btn(status, target), _btn(t(f"adm_feat_{key}"), target)]
        link = SETTINGS_LINKS.get(key)
        if link and can(link[2]):
            row.insert(0, _btn(t(link[0]), link[1]))
        elif key == "info_card_enabled":
            from paurubot.bot.routers.admin_general import theme_button

            row.insert(0, theme_button(t, cfg, origin))
        rows.append(row)
    if group == "main":
        from paurubot.bot.routers.admin_general import main_extra_rows

        rows += main_extra_rows(t, cfg)
    for label, link_target, permission in GROUP_LINKS.get(group, ()):
        if can(permission):
            rows.append([_btn(t(label), link_target)])
    rows.append(
        [
            _btn(t("btn_back"), Admin(section="feat", action="")),
            _btn(t("adm_ud_close"), Admin(section="gset", action="close")),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(Admin.filter(F.section == "feat"), StaffOnly("settings.write"))
async def features(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.bot.routers.admin_tools import _save_setting

    groups = list(FEATURES)
    if callback_data.action == "tog":
        if not 0 <= callback_data.id < len(FEATURE_KEYS) or callback_data.page not in {0, 1, 2}:
            await call.answer(t("button_expired"), show_alert=True)
            return
        key = FEATURE_KEYS[callback_data.id]
        group = next(g for g in groups if key in FEATURES[g])
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        current = bool(cfg.get(key))
        # page 0: an old button that simply flips; 1/2: the value it was drawn to set.
        value = not current if callback_data.page == 0 else callback_data.page == 2
        if value and not current and not can_enable(key, cfg):
            await call.answer(t("adm_feat_needs_setup"), show_alert=True)
            return
        if key == "bot_enabled" and current and not value:
            from paurubot.bot.routers.admin_general import MAIN, confirm_kb

            await call.answer()
            yes = Admin(section="gset", action="botoff", id=0, page=MAIN).pack()
            await show(call, t("adm_gs_bot_off_ask"), confirm_kb(t, yes))
            return
        if value != current:
            await _save_setting(session, shop, staff, key, value)
            await session.commit()
        await call.answer(t("adm_saved"))
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await show(call, group_text(t, group), group_kb(t, group, cfg, staff))
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if callback_data.action == "cat" and 0 <= callback_data.page < len(groups):
        group = groups[callback_data.page]
        await show(call, group_text(t, group), group_kb(t, group, cfg, staff))
        return
    await show(call, t("adm_feat_title"), features_kb(t, cfg))


# --- customer lists --------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "ulist"), StaffOnly("customers.read"))
async def user_lists(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    if callback_data.action not in {"f", "uf"}:
        await call.answer()
        kb = InlineKeyboardBuilder()
        for index, name in enumerate(USER_FILTERS):
            kb.button(text=t(f"adm_ulist_{name}"), callback_data=Admin(section="ulist", action="f", id=index))
        kb.adjust(2)
        kb.row(*_back(t, Admin(section="home")))
        await show(call, t("adm_ulist_title"), kb.as_markup())
        return
    if not 0 <= callback_data.id < len(USER_FILTERS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    await call.answer()
    name = USER_FILTERS[callback_data.id]
    conds = user_conditions(shop.id, name)
    total = int(await session.scalar(select(func.count(Customer.id)).where(*conds)) or 0)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(0, callback_data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(Customer).where(*conds).order_by(Customer.id.desc()).offset(page * PAGE).limit(PAGE)
        )
    )
    kb = InlineKeyboardBuilder()
    if rows:
        kb.row(
            *[
                InlineKeyboardButton(text=t(key), callback_data=Nav(to="noop").pack())
                for key in ("adm_ud_operations", "adm_ud_username", "adm_ud_id")
            ]
        )
    for row in rows:
        origin = ("uf" if callback_data.action == "uf" else "lf") + str(callback_data.id)
        username = ("@" + row.username) if row.username else t("adm_ud_no_username")
        kb.row(
            InlineKeyboardButton(
                text=t("adm_ud_manage"),
                callback_data=Admin(section="user", action=origin, id=row.id, page=page).pack(),
            ),
            InlineKeyboardButton(text=username[:20], callback_data=Nav(to="noop").pack()),
            InlineKeyboardButton(text=str(row.telegram_id), callback_data=Nav(to="noop").pack()),
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_previous"),
                callback_data=Admin(
                    section="ulist", action=callback_data.action, id=callback_data.id, page=page - 1
                ).pack(),
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_next"),
                callback_data=Admin(
                    section="ulist", action=callback_data.action, id=callback_data.id, page=page + 1
                ).pack(),
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(*_back(t, Admin(section="users" if callback_data.action == "uf" else "ulist")))
    kb.row(
        InlineKeyboardButton(
            text=t("adm_ud_close"), callback_data=Admin(section="uprofile", action="close").pack()
        )
    )
    text = t("adm_ulist_head", title=esc(t(f"adm_ulist_{name}")), total=total, page=page + 1, pages=pages)
    if not rows:
        text += "\n" + t("adm_ulist_empty")
    await show(call, text, kb.as_markup())


# --- period stats ----------------------------------------------------------------------------


async def period_stats(session: AsyncSession, shop_id: int, name: str) -> dict[str, Any]:
    start, end = period_range(name)
    done = [Order.shop_id == shop_id, Order.status == OrderStatus.COMPLETED]
    paid = [*done, Order.amount > 0, *_between(Order.completed_at, start, end)]

    async def one(stmt: Any) -> int:
        return int(await session.scalar(stmt) or 0)

    sales = await one(select(func.count(Order.id)).where(*paid))
    amount = await one(select(func.coalesce(func.sum(Order.amount), 0)).where(*paid))
    renew = [*paid, Order.kind == OrderKind.RENEW]
    users = await one(
        select(func.count(Customer.id)).where(Customer.shop_id == shop_id, Customer.telegram_id > 0)
    )
    buyers_ever = await one(
        select(func.count(func.distinct(Order.customer_id))).where(*done, Order.amount > 0)
    )
    week = await one(
        select(func.coalesce(func.sum(Order.amount), 0)).where(
            *done, Order.amount > 0, Order.completed_at >= datetime.now(UTC) - timedelta(days=7)
        )
    )
    return {
        "new_users": await one(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop_id,
                Customer.telegram_id > 0,
                *_between(Customer.created_at, start, end),
            )
        ),
        "buyers": await one(select(func.count(func.distinct(Order.customer_id))).where(*paid)),
        "sales": sales,
        "amount": money(amount),
        "renewals": await one(select(func.count(Order.id)).where(*renew)),
        "renew_amount": money(await one(select(func.coalesce(func.sum(Order.amount), 0)).where(*renew))),
        "trials": await one(
            select(func.count(Order.id)).where(
                *done, Order.kind == OrderKind.TRIAL, *_between(Order.completed_at, start, end)
            )
        ),
        "average": money(amount // sales if sales else 0),
        "forecast": money(week * 30 // 7),
        "users": users,
        "conversion": round(buyers_ever * 100 / users, 1) if users else 0,
        "active": await one(
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
            )
        ),
        "wallets": money(
            await one(
                select(func.coalesce(func.sum(LedgerAccount.balance), 0)).where(
                    LedgerAccount.shop_id == shop_id,
                    LedgerAccount.kind == LedgerAccountKind.WALLET,
                    LedgerAccount.customer_id.is_not(None),
                )
            )
        ),
        "pending": await one(
            select(func.count(Payment.id)).where(
                Payment.shop_id == shop_id, Payment.status == PaymentStatus.VERIFYING
            )
        ),
        "panels": await one(
            select(func.count(Panel.id)).where(Panel.shop_id == shop_id, Panel.is_active.is_(True))
        ),
        "resellers": await one(
            select(func.count(ResellerProfile.id)).where(
                ResellerProfile.shop_id == shop_id, ResellerProfile.is_active.is_(True)
            )
        ),
    }


@router.callback_query(Admin.filter(F.section == "pstats"), StaffOnly("reports.read"))
async def stats(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    index = callback_data.page if 0 <= callback_data.page < len(PERIODS) else 0
    name = PERIODS[index]
    await call.answer()
    data = await period_stats(session, shop.id, name)
    kb = InlineKeyboardBuilder()
    for i, period in enumerate(PERIODS):
        label = t(f"adm_pstats_{period}")
        kb.button(
            text=f"\u2705 {label}" if i == index else label, callback_data=Admin(section="pstats", page=i)
        )
    kb.adjust(3)
    kb.row(*_back(t, Admin(section="home")))
    await show(call, t("adm_pstats_text", period=esc(t(f"adm_pstats_{name}")), **data), kb.as_markup())
