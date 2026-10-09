"""Staff command deck: a pulse of shop figures and a hall of panels (MGMT-01..03).

Original layout, not a copy of any other bot: single-column cards with labeled
header rows, its own markers (deck / pulse / hall) and slice buttons for time
windows. Figures reuse admin_tools.stats_window/_slice_figures; panel probes
reuse provisioning.monitor.check_panel; every change is shop-scoped, audited
and answered.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.auth import staff as staff_service
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_tools import _slice_figures, stats_window
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ManageStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import settings as shop_settings
from paurubot.core.cards import luhn_ok, masked
from paurubot.core.formatting import esc, jdate, money, parse_int, size, to_latin_digits
from paurubot.db.base import utcnow
from paurubot.db.models import (
    BotText,
    Category,
    Customer,
    DiscountCode,
    DiscountKind,
    ForcedChannel,
    GiftCode,
    LedgerAccount,
    LedgerAccountKind,
    LogChannel,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    PanelKind,
    Plan,
    ResellerProfile,
    Service,
    ServiceStatus,
    Staff,
    StaffRole,
    SupportContact,
    Ticket,
    TicketStatus,
    TrialGrant,
    UsernameMethod,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.panels import PanelError
from paurubot.panels import drop as drop_driver
from paurubot.panels.registry import (
    EMERGENCY_OPTION,
    NATIONAL_NET_OPTION,
    visible_in_shop,
)
from paurubot.panels.registry import build as build_driver
from paurubot.payments import fx as fx_rates
from paurubot.provisioning import monitor
from paurubot.security import secretbox

router = Router(name="manage")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

SLICES = ("1h", "today", "yesterday", "month", "lastmonth", "custom")
SLICE_KEYS = {
    "1h": "mgmt_slice_1h",
    "today": "mgmt_slice_today",
    "yesterday": "mgmt_slice_yesterday",
    "month": "mgmt_slice_month",
    "lastmonth": "mgmt_slice_lastmonth",
    "custom": "mgmt_slice_custom",
}
SHOWCASE_DEFAULTS: dict[str, bool] = {
    "show_in_shop": True,
    "show_trial": True,
    "allow_renew": True,
    "allow_extra": True,
    "allow_move": True,
    "show_price": True,
    "report_issue": True,
    "direct_buy": False,
}
SHOWCASE_LABELS = {
    "show_in_shop": "mgmt_flag_show_in_shop",
    "show_trial": "mgmt_flag_show_trial",
    "allow_renew": "mgmt_flag_allow_renew",
    "allow_extra": "mgmt_flag_allow_extra",
    "allow_move": "mgmt_flag_allow_move",
    "show_price": "mgmt_flag_show_price",
    "report_issue": "mgmt_flag_report_issue",
    "direct_buy": "mgmt_flag_direct_buy",
}
SHOWCASE_KEYS = tuple(SHOWCASE_DEFAULTS)
EXTRA_KINDS = ("gb", "day")
SELL_KEYS = ("sell_extra_volume", "sell_extra_days")


# --- pure helpers (unit-tested without a database) ---------------------------------


def slice_last_month(now: datetime) -> tuple[datetime, datetime]:
    """First day of the previous month -> first day of the current one."""
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first_this.month == 1:
        first_prev = first_this.replace(year=first_this.year - 1, month=12)
    else:
        first_prev = first_this.replace(month=first_this.month - 1)
    return first_prev, first_this


def parse_custom_date(text: str | None) -> datetime | None:
    """One Gregorian day as YYYY-MM-DD at midnight UTC; None when it does not fit."""
    parts = to_latin_digits(text or "").strip().split("-")
    if len(parts) != 3 or len(parts[0]) != 4:
        return None
    try:
        return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=UTC)
    except ValueError:
        return None


def showcase_flags(options: dict[str, Any] | None) -> dict[str, bool]:
    """Effective showcase flags: stored values over the defaults."""
    raw = (options or {}).get("showcase")
    if not isinstance(raw, dict):
        raw = {}
    return {key: bool(raw.get(key, default)) for key, default in SHOWCASE_DEFAULTS.items()}


def format_ping(latency_ms: int | None) -> str:
    """A latency reading for the health card; "-" when the probe never answered."""
    return "-" if latency_ms is None else f"{int(latency_ms)} ms"


@dataclass(slots=True)
class Pulse:
    period: str
    sales: int
    sales_amount: int
    topups: int
    topup_amount: int
    joined: int
    customers: int
    buyers: int
    conversion: str
    tests: int
    active: int
    renewals: int
    forecast: int
    panels: int
    healthy: int
    res_l1: int
    res_l2: int
    wallet: int
    bot: str
    db: str


def pulse_text(t: Texts, pulse: Pulse) -> str:
    """Render collected figures through the locale catalog (no database)."""
    return "\n\n".join(
        [
            t("mgmt_pulse_title", period=pulse.period),
            t(
                "mgmt_pulse_trade",
                sales=pulse.sales,
                sales_amount=money(pulse.sales_amount),
                topups=pulse.topups,
                topup_amount=money(pulse.topup_amount),
            ),
            t(
                "mgmt_pulse_audience",
                customers=pulse.customers,
                joined=pulse.joined,
                buyers=pulse.buyers,
                conversion=pulse.conversion,
                tests=pulse.tests,
            ),
            t(
                "mgmt_pulse_services",
                active=pulse.active,
                renewals=pulse.renewals,
                forecast=money(pulse.forecast),
            ),
            t(
                "mgmt_pulse_fleet",
                panels=pulse.panels,
                healthy=pulse.healthy,
                res_l1=pulse.res_l1,
                res_l2=pulse.res_l2,
            ),
            t("mgmt_pulse_health", bot=pulse.bot, db=pulse.db, wallet=money(pulse.wallet)),
        ]
    )


# --- shared bits -------------------------------------------------------------------


def _can_view(staff: StaffIdentity) -> bool:
    return staff.can("dashboard.view") or staff.can("reports.read")


def _slice_label(t: Texts, key: str) -> str:
    return t(SLICE_KEYS.get(key, "mgmt_slice_today"))


def hub_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("mgmt_btn_pulse"), callback_data=Admin(section="manage", action="pulse"))
    kb.button(text=t("mgmt_btn_panels"), callback_data=Admin(section="manage", action="panels"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def pulse_kb(t: Texts, current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in SLICES:
        mark = "● " if key == current else ""
        kb.button(
            text=f"{mark}{_slice_label(t, key)}"[:64],
            callback_data=Admin(section="manage", action="pulse", page=SLICES.index(key)),
        )
    kb.button(text=t("mgmt_btn_panels"), callback_data=Admin(section="manage", action="panels"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage"))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def pulse_window(key: str, now: datetime, custom: datetime | None = None) -> tuple[datetime, datetime | None]:
    """(since, until) for a deck slice; `until` is None for open ranges."""
    if key == "lastmonth":
        start, end = slice_last_month(now)
        return start, end
    if key == "custom" and custom is not None:
        return custom, custom + timedelta(days=1)
    if key == "custom":
        return stats_window("today", now)
    return stats_window(key, now)


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


async def _bot_latency(bot: Bot) -> int | None:
    started = time.monotonic()
    try:
        await bot.get_me()
    except Exception:
        return None
    return int((time.monotonic() - started) * 1000)


async def _db_latency(session: AsyncSession) -> int | None:
    started = time.monotonic()
    try:
        await session.execute(select(1))
    except Exception:
        return None
    return int((time.monotonic() - started) * 1000)


async def collect_pulse(
    session: AsyncSession,
    shop_id: int,
    since: datetime,
    until: datetime | None,
    *,
    now: datetime,
    period: str,
    bot_ms: int | None,
    db_ms: int | None,
) -> Pulse:
    """Every figure on the pulse card for one window."""
    sales, sales_amount, topups, topup_amount, joined = await _slice_figures(session, shop_id, since, until)
    buyers_q = select(func.count(func.distinct(Order.customer_id))).where(
        Order.shop_id == shop_id,
        Order.status == OrderStatus.COMPLETED,
        Order.amount > 0,
        Order.completed_at >= since,
    )
    trials_q = select(func.count(TrialGrant.id)).where(
        TrialGrant.shop_id == shop_id, TrialGrant.created_at >= since
    )
    renewals_q = select(func.count(Order.id)).where(
        Order.shop_id == shop_id,
        Order.status == OrderStatus.COMPLETED,
        Order.kind == OrderKind.RENEW,
        Order.completed_at >= since,
    )
    if until is not None:
        buyers_q = buyers_q.where(Order.completed_at < until)
        trials_q = trials_q.where(TrialGrant.created_at < until)
        renewals_q = renewals_q.where(Order.completed_at < until)
    buyers = await _count(session, buyers_q)
    month_since, _ = stats_window("month", now)
    _, month_amount, _, _, _ = await _slice_figures(session, shop_id, month_since, None)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    forecast = month_amount * days_in_month // max(1, now.day)
    return Pulse(
        period=period,
        sales=sales,
        sales_amount=sales_amount,
        topups=topups,
        topup_amount=topup_amount,
        joined=joined,
        customers=await _count(
            session,
            select(func.count(Customer.id)).where(Customer.shop_id == shop_id, Customer.telegram_id > 0),
        ),
        buyers=buyers,
        conversion=f"{100.0 * buyers / joined:.1f}%" if joined else "0.0%",
        tests=await _count(session, trials_q),
        active=await _count(
            session,
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
            ),
        ),
        renewals=await _count(session, renewals_q),
        forecast=forecast,
        panels=await _count(session, select(func.count(Panel.id)).where(Panel.shop_id == shop_id)),
        healthy=await _count(
            session,
            select(func.count(Panel.id)).where(
                Panel.shop_id == shop_id, Panel.is_active.is_(True), Panel.is_healthy.is_(True)
            ),
        ),
        res_l1=await _count(
            session,
            select(func.count(ResellerProfile.id)).where(
                ResellerProfile.shop_id == shop_id,
                ResellerProfile.is_active.is_(True),
                ResellerProfile.parent_id.is_(None),
            ),
        ),
        res_l2=await _count(
            session,
            select(func.count(ResellerProfile.id)).where(
                ResellerProfile.shop_id == shop_id,
                ResellerProfile.is_active.is_(True),
                ResellerProfile.parent_id.is_not(None),
            ),
        ),
        wallet=await _count(
            session,
            select(func.coalesce(func.sum(LedgerAccount.balance), 0)).where(
                LedgerAccount.shop_id == shop_id, LedgerAccount.kind == LedgerAccountKind.WALLET
            ),
        ),
        bot=format_ping(bot_ms),
        db=format_ping(db_ms),
    )


async def _scoped_panel(session: AsyncSession, shop: ShopRow, panel_id: int) -> Panel | None:
    panel = await session.get(Panel, panel_id)
    if panel is None or panel.shop_id != shop.id:
        return None
    return panel


def _health_dot(panel: Panel) -> str:
    if not panel.is_active:
        return "⚪️"
    if panel.is_healthy is True:
        return "🟢"
    if panel.is_healthy is False:
        return "🔴"
    return "🟡"


# --- hub ---------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "manage"))
async def deck(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    if not _can_view(staff):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    action = callback_data.action
    if action in ("show", "hub"):
        await _fx_hub(call, t, staff)
        return
    if action == "fxhub":
        await _fx_section(call, callback_data, session, shop, t, staff, bot, cfg, state)
        return
    if action == "fxstats":
        if not _can_view(staff):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        key = (
            FX_STATS_SLICES[callback_data.page] if 0 <= callback_data.page < len(FX_STATS_SLICES) else "today"
        )
        if key == "custom":
            await call.answer()
            await state.set_state(ManageStates.range_start)
            if isinstance(call.message, Message):
                await call.message.answer(t("fx_range_ask_start"), reply_markup=keyboards.cancel(t))
            return
        await _fx_show_stats(call, session, shop, t, bot, key)
        return
    if action == "fxclose":
        await _fx_close(call, t)
        return
    if action == "fxpanels":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await call.answer()
        await show(call, t("fx_panels_menu_title"), fx_panels_kb(t))
        return
    if action == "fxpick":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _panels(call, session, shop, t, back="fxpanels", detail="fxpanel")
        return
    if action == "fxadd":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await call.answer()
        await show(call, t("fx_add_kind"), fx_kind_kb(t))
        return
    if action == "fxkind":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        kinds = list(PanelKind)
        if not 0 <= callback_data.page < len(kinds):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await call.answer()
        await state.update_data(kind=kinds[callback_data.page].value)
        await state.set_state(ManageStates.panel_add_name)
        if isinstance(call.message, Message):
            await call.message.answer(t("fx_add_name"), reply_markup=keyboards.cancel(t))
        return
    if action in ("fxqtime", "fxqvol"):
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _quick_start(call, action, t, cfg, state)
        return
    if action == "fxpanel":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _panel(call, callback_data, session, shop, t, staff, cfg, mirror=True)
        return
    if action in ("fxextras", "fxflags"):
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        panel = await _scoped_panel(session, shop, callback_data.id)
        if panel is None:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await call.answer()
        if action == "fxextras":
            await show(call, _extras_text(t, panel, cfg), _fx_extras_kb(t, panel))
        else:
            await show(call, t("mgmt_flags_title"), _fx_flags_kb(t, panel))
        return
    if action in MIRROR_MUT:
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _mutate(
            call,
            Admin(section="manage", action=MIRROR_MUT[action], id=callback_data.id, page=callback_data.page),
            session,
            shop,
            t,
            staff,
            cfg,
            state,
            mirror=True,
        )
        return
    if action in ("fxdesk", "fxdeskrefresh"):
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_desk(call, callback_data, session, shop, t, cfg, refresh=action == "fxdeskrefresh")
        return
    if action in ("fxtest", "fxtesttoggle", "fxtestreset"):
        need = "panels.write" if action != "fxtest" else "panels.read"
        if not staff.can(need):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_test(call, callback_data, session, shop, t, staff, action)
        return
    if action in ("fxnat", "fxemg", "fxip", "fxrenew"):
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_toggle(call, callback_data, session, shop, t, staff, cfg, action)
        return
    if action in ("fxcap", "fxagent", "fxmove", "fxaddr"):
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_value_ask(call, callback_data, session, shop, t, cfg, action, state)
        return
    if action == "fxlimits":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_limits(call, callback_data, session, shop, t)
        return
    if action == "fxlimit":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_limit_ask(call, callback_data, session, shop, t, state)
        return
    if action == "fxhidden":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_hidden_show(call, callback_data, session, shop, t)
        return
    if action == "fxhiddenmode":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_hidden_ask(call, callback_data, session, shop, t, state)
        return
    if action == "fxkey":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _fx_key_ask(call, callback_data, session, shop, t, state)
        return
    if action == "pulse":
        key = SLICES[callback_data.page] if 0 <= callback_data.page < len(SLICES) else "today"
        if key == "custom":
            await call.answer()
            await state.set_state(ManageStates.custom_date)
            if isinstance(call.message, Message):
                await call.message.answer(t("mgmt_custom_ask"), reply_markup=keyboards.cancel(t))
            return
        await _pulse(call, session, shop, t, bot, key)
        return
    if action == "panels":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _panels(call, session, shop, t)
        return
    if action == "panel":
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _panel(call, callback_data, session, shop, t, staff, cfg)
        return
    if action in ("extras", "flags"):
        if not staff.can("panels.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        panel = await _scoped_panel(session, shop, callback_data.id)
        if panel is None:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await call.answer()
        if action == "extras":
            await show(call, _extras_text(t, panel, cfg), _extras_kb(t, panel))
        else:
            await show(call, t("mgmt_flags_title"), _flags_kb(t, panel))
        return
    if action in ("toggle", "check", "rename", "delete", "extra_price", "sell", "umethod", "flag"):
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _mutate(call, callback_data, session, shop, t, staff, cfg, state)
        return
    if action.startswith("w2"):
        await _w2_dispatch(call, callback_data, session, shop, t, staff, cfg, state, bot)
        return
    await call.answer()


async def _fx_hub(call: CallbackQuery, t: Texts, staff: StaffIdentity) -> None:
    if not _can_view(staff):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    await show(call, t("fx_hub_title", date=jdate(utcnow())), fx_hub_kb(t))


async def _pulse(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, bot: Bot, key: str
) -> None:
    await call.answer()
    now = utcnow()
    since, until = pulse_window(key, now)
    period = _slice_label(t, key)
    pulse = await collect_pulse(
        session,
        shop.id,
        since,
        until,
        now=now,
        period=period,
        bot_ms=await _bot_latency(bot),
        db_ms=await _db_latency(session),
    )
    await show(call, pulse_text(t, pulse), pulse_kb(t, key))


@router.message(ManageStates.custom_date, F.text)
async def custom_slice(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    if not _can_view(staff):
        await state.clear()
        await message.answer(t("adm_forbidden"), reply_markup=menu(t, cfg, staff))
        return
    day = parse_custom_date(message.text)
    if day is None:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.clear()
    since, until = pulse_window("custom", utcnow(), day)
    pulse = await collect_pulse(
        session,
        shop.id,
        since,
        until,
        now=utcnow(),
        period=jdate(day),
        bot_ms=await _bot_latency(bot),
        db_ms=await _db_latency(session),
    )
    await message.answer(t("mgmt_pulse_title", period=jdate(day)), reply_markup=menu(t, cfg, staff))
    await message.answer(pulse_text(t, pulse), reply_markup=pulse_kb(t, "custom"))


# --- panel hall --------------------------------------------------------------------


async def _service_counts(session: AsyncSession, shop_id: int) -> dict[int, int]:
    rows = (
        await session.execute(
            select(Service.panel_id, func.count(Service.id))
            .where(Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE)
            .group_by(Service.panel_id)
        )
    ).all()
    return {int(panel_id): int(count) for panel_id, count in rows}


async def _panels(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    back: str = "hub",
    detail: str = "panel",
) -> None:
    panels = list(await session.scalars(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id)))
    counts = await _service_counts(session, shop.id)
    healthy = sum(1 for p in panels if p.is_active and p.is_healthy is True)
    lines = [t("mgmt_panels_title", count=len(panels), healthy=healthy)]
    kb = InlineKeyboardBuilder()
    for panel in panels:
        note = "" if visible_in_shop(panel) else f" — {t('mgmt_shop_hidden')}"
        row = f"{_health_dot(panel)} {esc(panel.name)} · {panel.kind.value}"
        lines.append(f"{row} · {counts.get(panel.id, 0)}{note}")
        kb.button(
            text=f"{_health_dot(panel)} {panel.name}"[:64],
            callback_data=Admin(section="manage", action=detail, id=panel.id),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action=back))
    kb.adjust(1)
    await call.answer()
    await show(call, "\n".join(lines), kb.as_markup())


def _panel_text(
    t: Texts, panel: Panel, dist: list[tuple[ServiceStatus, int]], money_line: str, cfg: dict[str, Any]
) -> str:
    if not panel.is_active:
        state = t("mgmt_state_off")
    elif panel.is_healthy is True:
        state = t("mgmt_state_ok")
    elif panel.is_healthy is False:
        state = t("mgmt_state_down")
    else:
        state = t("mgmt_state_unknown")
    stats = panel.stats if isinstance(panel.stats, dict) else {}
    version = esc(str(stats.get("version") or "-"))
    users_total = stats.get("users_total")
    users_active = stats.get("users_active")
    if isinstance(users_total, (int, float)) and isinstance(users_active, (int, float)):
        users = f"{int(users_active)}/{int(users_total)}"
    elif isinstance(users_total, (int, float)):
        users = str(int(users_total))
    else:
        users = "-"
    cpu = stats.get("cpu_percent")
    cpu_text = f"{cpu}%" if isinstance(cpu, (int, float)) else "-"
    mem_used, mem_total = stats.get("mem_used"), stats.get("mem_total")
    if isinstance(mem_used, (int, float)) and isinstance(mem_total, (int, float)):
        mem = f"{size(int(mem_used))}/{size(int(mem_total))}"
    else:
        mem = "-"
    parts = [
        t("mgmt_panel_title", name=esc(panel.name), kind=panel.kind.value),
        t(
            "mgmt_panel_conn",
            state=state,
            error=esc(str(panel.health_error or "-")[:120]),
            down=jdate(panel.down_since, with_time=True) if panel.down_since else "-",
        ),
        t("mgmt_panel_stats", version=version, users=users, cpu=cpu_text, mem=mem),
    ]
    for status, count in dist:
        parts.append(f"{keyboards.STATUS_ICON.get(status, '')} {t(f'status_{status.value}')}: {count}")
    parts.append(money_line)
    parts.append(_extras_text(t, panel, cfg))
    parts.append(t("mgmt_umethod_line", method=panel.username_method.value))
    return "\n\n".join(parts)


def _extras_text(t: Texts, panel: Panel, cfg: dict[str, Any]) -> str:
    options = panel.options if isinstance(panel.options, dict) else {}
    gb = options.get("extra_gb_price", cfg.get("extra_gb_price"))
    day = options.get("extra_day_price", cfg.get("extra_day_price"))
    return t(
        "mgmt_panel_extras",
        gb=money(gb if isinstance(gb, int) else None),
        day=money(day if isinstance(day, int) else None),
        sell_vol="✅" if options.get("sell_extra_volume", True) else "❌",
        sell_days="✅" if options.get("sell_extra_days", True) else "❌",
    )


def _panel_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("mgmt_btn_check"), callback_data=Admin(section="manage", action="check", id=panel.id))
    key = "mgmt_btn_disable" if panel.is_active else "mgmt_btn_enable"
    kb.button(text=t(key), callback_data=Admin(section="manage", action="toggle", id=panel.id))
    kb.button(text=t("mgmt_btn_rename"), callback_data=Admin(section="manage", action="rename", id=panel.id))
    kb.button(text=t("mgmt_btn_extras"), callback_data=Admin(section="manage", action="extras", id=panel.id))
    kb.button(
        text=t("mgmt_btn_umethod"), callback_data=Admin(section="manage", action="umethod", id=panel.id)
    )
    kb.button(text=t("mgmt_btn_flags"), callback_data=Admin(section="manage", action="flags", id=panel.id))
    kb.button(text=t("mgmt_btn_delete"), callback_data=Admin(section="manage", action="delete", id=panel.id))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="panels"))
    kb.adjust(1)
    return kb.as_markup()


def _extras_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("mgmt_btn_extra_gb"), callback_data=Admin(section="manage", action="extra_price", id=panel.id)
    )
    kb.button(
        text=t("mgmt_btn_extra_day"),
        callback_data=Admin(section="manage", action="extra_price", id=panel.id, page=1),
    )
    kb.button(
        text=t("mgmt_btn_sell_volume"), callback_data=Admin(section="manage", action="sell", id=panel.id)
    )
    kb.button(
        text=t("mgmt_btn_sell_days"),
        callback_data=Admin(section="manage", action="sell", id=panel.id, page=1),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="panel", id=panel.id))
    kb.adjust(1)
    return kb.as_markup()


def _flags_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    flags = showcase_flags(panel.options if isinstance(panel.options, dict) else {})
    kb = InlineKeyboardBuilder()
    for index, key in enumerate(SHOWCASE_KEYS):
        mark = "✅" if flags[key] else "❌"
        kb.button(
            text=f"{mark} {t(SHOWCASE_LABELS[key])}"[:64],
            callback_data=Admin(section="manage", action="flag", id=panel.id, page=index),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="panel", id=panel.id))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


async def _panel_money(session: AsyncSession, shop: ShopRow, panel: Panel) -> dict[str, int]:
    base = (
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
        .select_from(Order)
        .join(Service, Service.id == Order.service_id)
        .where(
            Order.shop_id == shop.id,
            Order.status == OrderStatus.COMPLETED,
            Service.panel_id == panel.id,
        )
    )
    orders, amount = (await session.execute(base)).one()
    renewals = int(await session.scalar(base.where(Order.kind == OrderKind.RENEW)) or 0)
    return {"orders": int(orders), "amount": int(amount), "renewals": renewals}


async def _panel(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    mirror: bool = False,
) -> None:
    del staff
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    dist_rows = (
        await session.execute(
            select(Service.status, func.count(Service.id))
            .where(
                Service.shop_id == shop.id,
                Service.panel_id == panel.id,
                Service.status != ServiceStatus.DELETED,
            )
            .group_by(Service.status)
        )
    ).all()
    figures = await _panel_money(session, shop, panel)
    await call.answer()
    base = _panel_text(
        t,
        panel,
        [(row[0], int(row[1])) for row in dist_rows],
        t(
            "mgmt_panel_money",
            orders=figures["orders"],
            amount=money(figures["amount"]),
            renewals=figures["renewals"],
        ),
        cfg,
    )
    if mirror:
        await show(call, f"{base}\n\n{_fx_leaves_text(t, panel, cfg)}", _fx_panel_kb(t, panel))
        return
    await show(
        call,
        base,
        _panel_kb(t, panel),
    )


# --- mutations -----------------------------------------------------------------------


async def _mutate(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    mirror: bool = False,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    action = callback_data.action
    if action == "toggle":
        panel.is_active = not panel.is_active
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.toggle",
            entity="panel",
            entity_id=panel.id,
            after={"is_active": panel.is_active},
        )
        await session.commit()
        await drop_driver(panel.id)
        await call.answer(t("adm_saved"))
        await _panel(
            call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg, mirror
        )
        return
    if action == "check":
        try:
            result = await monitor.check_panel(session, panel)
        except PanelError as exc:
            await call.answer(t("adm_panel_error"), show_alert=True)
            panel.health_error = str(exc)[:255]
            await session.commit()
            return
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.check",
            entity="panel",
            entity_id=panel.id,
            after={"ok": result.ok, "latency_ms": result.latency_ms},
        )
        await session.commit()
        await call.answer(t("adm_saved") if result.ok else t("adm_panel_error"), show_alert=not result.ok)
        await _panel(
            call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg, mirror
        )
        return
    if action == "rename":
        await call.answer()
        await state.set_state(ManageStates.rename)
        await state.update_data(panel_id=panel.id)
        if isinstance(call.message, Message):
            await call.message.answer(
                t("mgmt_rename_ask", current=esc(panel.name)), reply_markup=keyboards.cancel(t)
            )
        return
    if action == "delete":
        await _delete(call, callback_data, session, shop, t, staff, panel, mirror)
        return
    if action == "extra_price":
        kind = EXTRA_KINDS[callback_data.page] if 0 <= callback_data.page < len(EXTRA_KINDS) else "gb"
        await call.answer()
        await state.set_state(ManageStates.extra_price)
        await state.update_data(panel_id=panel.id, kind=kind)
        options = panel.options if isinstance(panel.options, dict) else {}
        current = options.get(f"extra_{kind}_price", cfg.get(f"extra_{kind}_price"))
        if isinstance(call.message, Message):
            await call.message.answer(
                t("mgmt_extra_ask", kind=kind, current=money(current if isinstance(current, int) else None)),
                reply_markup=keyboards.cancel(t),
            )
        return
    if action == "sell":
        key = SELL_KEYS[callback_data.page] if 0 <= callback_data.page < len(SELL_KEYS) else SELL_KEYS[0]
        options = dict(panel.options if isinstance(panel.options, dict) else {})
        options[key] = not bool(options.get(key, True))
        panel.options = options
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.extras",
            entity="panel",
            entity_id=panel.id,
            after={key: options[key]},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        extras_kb = _fx_extras_kb(t, panel) if mirror else _extras_kb(t, panel)
        await show(call, _extras_text(t, panel, cfg), extras_kb)
        return
    if action == "umethod":
        order = [item.value for item in UsernameMethod]
        method = panel.username_method
        current = method.value if isinstance(method, UsernameMethod) else order[0]
        following = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
        before = method.value if isinstance(method, UsernameMethod) else current
        panel.username_method = UsernameMethod(following)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.username_method",
            entity="panel",
            entity_id=panel.id,
            before={"username_method": before},
            after={"username_method": following},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _panel(
            call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg, mirror
        )
        return
    if action == "flag":
        if not 0 <= callback_data.page < len(SHOWCASE_KEYS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        flag = SHOWCASE_KEYS[callback_data.page]
        options = dict(panel.options if isinstance(panel.options, dict) else {})
        case = dict(options.get("showcase") or {})
        case[flag] = not showcase_flags(options)[flag]
        options["showcase"] = case
        panel.options = options
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.showcase",
            entity="panel",
            entity_id=panel.id,
            after={flag: case[flag]},
        )
        await session.commit()
        await drop_driver(panel.id)
        await call.answer(t("adm_saved"))
        flags_kb = _fx_flags_kb(t, panel) if mirror else _flags_kb(t, panel)
        await show(call, t("mgmt_flags_title"), flags_kb)
        return
    await call.answer()


async def _delete(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    panel: Panel,
    mirror: bool = False,
) -> None:
    detail = "fxpanel" if mirror else "panel"
    confirm = "fxdelete" if mirror else "delete"
    if not callback_data.page:
        await call.answer()
        await show(
            call,
            t("mgmt_delete_ask", name=esc(panel.name)),
            keyboards.confirm(
                t,
                Admin(section="manage", action=confirm, id=panel.id, page=1).pack(),
                Admin(section="manage", action=detail, id=panel.id).pack(),
            ),
        )
        return
    # Services hold a RESTRICT foreign key to their panel, so any live row blocks the delete.
    live = await _count(
        session,
        select(func.count(Service.id)).where(
            Service.shop_id == shop.id,
            Service.panel_id == panel.id,
            Service.status != ServiceStatus.DELETED,
        ),
    )
    if live:
        await call.answer(t("mgmt_delete_refused", active=live), show_alert=True)
        await show(
            call,
            t("mgmt_delete_refused", active=live),
            keyboards.back(t, Admin(section="manage", action=detail, id=panel.id).pack()),
        )
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.delete",
        entity="panel",
        entity_id=panel.id,
        before={"name": panel.name},
    )
    await session.delete(panel)
    await session.commit()
    await drop_driver(panel.id)
    await call.answer(t("adm_saved"))
    await _panels(call, session, shop, t, back="fxpanels" if mirror else "hub", detail=detail)


@router.message(ManageStates.rename, F.text)
async def rename_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    panel = await _scoped_panel(session, shop, int(data.get("panel_id") or 0))
    name = (message.text or "").strip()
    if panel is None or not 1 <= len(name) <= 64:
        if panel is None:
            await state.clear()
            await message.answer(t("mgmt_not_found"), reply_markup=menu(t, cfg, staff))
            return
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.clear()
    before = panel.name
    panel.name = name
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.rename",
        entity="panel",
        entity_id=panel.id,
        before={"name": before},
        after={"name": name},
    )
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


@router.message(ManageStates.extra_price, F.text)
async def extra_price_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    panel = await _scoped_panel(session, shop, int(data.get("panel_id") or 0))
    kind = str(data.get("kind") or "gb")
    value = parse_int(message.text)
    if panel is None or kind not in EXTRA_KINDS or value is None or not 0 <= value <= 100_000_000:
        if panel is None:
            await state.clear()
            await message.answer(t("mgmt_not_found"), reply_markup=menu(t, cfg, staff))
            return
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.clear()
    key = f"extra_{kind}_price"
    options = dict(panel.options if isinstance(panel.options, dict) else {})
    before = options.get(key)
    options[key] = value
    panel.options = options
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.extras",
        entity="panel",
        entity_id=panel.id,
        before={key: before},
        after={key: value},
    )
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(_extras_text(t, panel, cfg), reply_markup=_extras_kb(t, panel))


# --- Faoxima mirror hub, stats, panels and leaves (MGMT-10..12) ----------------------
# A native reimplementation of the competitor's Telegram-bot management hub: the same
# menu shape (hub, stats slices, panel add/manage, per-panel leaves) mapped onto our
# backend (pulse figures, panel registry, shop settings, FX helpers). Persian button
# labels live in the locale catalog under fx_* keys; this module only references them.

HUB_SECTIONS: tuple[tuple[str, str], ...] = (
    ("stats", "fx_hub_stats"),
    ("panels", "fx_hub_panels"),
    ("shop", "fx_hub_shop"),
    ("finance", "fx_hub_finance"),
    ("users", "fx_hub_users"),
    ("support", "fx_hub_support"),
    ("help", "fx_hub_help"),
    ("channel", "fx_hub_channel"),
    ("tech", "fx_hub_tech"),
)
STUBBED_SECTIONS = frozenset({"shop", "finance", "users", "support", "help", "channel", "tech"})

FX_STATS_SLICES = ("all", "1h", "today", "yesterday", "month", "lastmonth", "custom")
FX_SLICE_KEYS = {
    "all": "fx_stats_all",
    "1h": "fx_stats_1h",
    "today": "fx_stats_today",
    "yesterday": "fx_stats_yesterday",
    "month": "fx_stats_month",
    "lastmonth": "fx_stats_lastmonth",
    "custom": "fx_stats_custom",
}
ALL_SINCE = datetime(2020, 1, 1, tzinfo=UTC)

TEST_CFG_DEFAULTS: dict[str, Any] = {"enabled": True, "days": 1, "gb": 1, "max_per_user": 1}

LIMIT_DEFS: tuple[tuple[str, int, int], ...] = (
    ("min_custom_gb", 1, 100000),
    ("max_custom_gb", 1, 100000),
    ("min_custom_days", 1, 3650),
    ("max_custom_days", 1, 3650),
)

QUICK_SPEC = {"time": "quick_time_prices", "volume": "quick_volume_prices"}

MIRROR_MUT = {
    "fxtoggle": "toggle",
    "fxcheck": "check",
    "fxrename": "rename",
    "fxdelete": "delete",
    "fxextraprice": "extra_price",
    "fxsell": "sell",
    "fxumethod": "umethod",
    "fxflag": "flag",
}

VALUE_LABELS = {
    "fxcap": "fx_pd_capacity",
    "fxagent": "fx_pd_agent",
    "fxmove": "fx_pd_move",
    "fxaddr": "fx_pd_addr",
}


# --- pure helpers (unit-tested without a database) ---------------------------------


def hidden_ids(options: dict[str, Any] | None) -> list[int]:
    """Telegram IDs kept out of this panel (the `hide_user` option); junk is ignored."""
    raw = (options or {}).get("hide_user")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for value in raw:
        if isinstance(value, bool):
            continue
        try:
            tid = int(value)
        except (TypeError, ValueError):
            continue
        if tid > 0 and tid not in out:
            out.append(tid)
    return out


def with_hidden_added(options: dict[str, Any] | None, telegram_id: int) -> dict[str, Any]:
    """Copy of `options` with `telegram_id` on the `hide_user` list."""
    next_options = dict(options or {})
    ids = hidden_ids(next_options)
    if telegram_id > 0 and telegram_id not in ids:
        ids.append(telegram_id)
    next_options["hide_user"] = ids
    return next_options


def with_hidden_removed(options: dict[str, Any] | None, telegram_id: int) -> dict[str, Any]:
    """Copy of `options` without `telegram_id` on the `hide_user` list."""
    next_options = dict(options or {})
    next_options["hide_user"] = [tid for tid in hidden_ids(next_options) if tid != telegram_id]
    return next_options


def trial_cfg(options: dict[str, Any] | None) -> dict[str, Any]:
    """Effective trial-desk config: the stored `test_cfg` values over the defaults."""
    merged = dict(TEST_CFG_DEFAULTS)
    raw = (options or {}).get("test_cfg")
    if not isinstance(raw, dict):
        return merged
    merged["enabled"] = bool(raw.get("enabled", merged["enabled"]))
    for key in ("days", "gb", "max_per_user"):
        try:
            merged[key] = max(0, int(raw.get(key, merged[key])))
        except (TypeError, ValueError):
            continue
    return merged


def parse_quick_price(text: str | None) -> int | None:
    """One quick-price tier in Toman; None when it does not fit."""
    value = parse_int(text)
    if value is None or not 0 <= value <= 100_000_000:
        return None
    return value


def quick_current(values: Any) -> str:
    """Three stored tiers as `a / b / c`; `-` when nothing is stored yet."""
    if not isinstance(values, dict):
        return "-"
    return " / ".join(str(values.get(key, "-")) for key in ("tier1", "tier2", "tier3"))


def fx_effective(rate_value: int, markup_percent: int) -> int:
    """A rate with the shop's FX markup applied (mirrors payments.fx.rate)."""
    return max(0, int(rate_value)) * (100 + max(0, int(markup_percent))) // 100


def renewal_share(renewals: int, sales: int) -> str:
    """Renewals as a share of sales for the full-stats card."""
    return f"{100.0 * renewals / sales:.1f}%" if sales else "0.0%"


def full_pulse_text(t: Texts, pulse: Pulse) -> str:
    """Pulse card plus the mirror's averages line (average per customer, renewal share)."""
    avg = money(pulse.sales_amount // pulse.customers) if pulse.customers else money(0)
    extra = t("fx_full_extra", avg=avg, renewal_pct=renewal_share(pulse.renewals, pulse.sales))
    return f"{pulse_text(t, pulse)}\n\n{extra}"


def panel_renew_mode(options: dict[str, Any] | None, cfg: dict[str, Any]) -> str:
    """Effective per-panel renewal mode, mirroring provisioning.lifecycle.renew_mode."""
    reset = (options or {}).get("renew_resets_usage")
    if reset is not None:
        return "extend_reset" if reset else "extend_carry"
    if cfg.get("renew_mode") in ("extend_reset", "extend_carry", "restart_reset", "restart_carry"):
        return str(cfg["renew_mode"])
    return "extend_reset" if cfg.get("renew_resets_usage", True) else "extend_carry"


def capacity_of(options: dict[str, Any] | None) -> int | None:
    """Per-panel account cap from `capacity_limit`; None means unlimited (cf. lifecycle)."""
    try:
        limit = int((options or {}).get("capacity_limit") or 0)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


# --- mirror keyboards ----------------------------------------------------------------


def fx_hub_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, (_, key) in enumerate(HUB_SECTIONS):
        kb.button(text=t(key), callback_data=Admin(section="manage", action="fxhub", page=index))
    kb.button(text=t("fx_hub_home"), callback_data=Admin(section="home"))
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def fx_stats_kb(t: Texts, current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, key in enumerate(FX_STATS_SLICES):
        mark = "● " if key == current else ""
        kb.button(
            text=f"{mark}{t(FX_SLICE_KEYS[key])}"[:64],
            callback_data=Admin(section="manage", action="fxstats", page=index),
        )
    kb.button(text=t("fx_stats_close"), callback_data=Admin(section="manage", action="fxclose"))
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


def fx_panels_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("fx_pn_manage"), callback_data=Admin(section="manage", action="fxpick"))
    kb.button(text=t("fx_pn_add"), callback_data=Admin(section="manage", action="fxadd"))
    kb.button(text=t("fx_pn_qtime"), callback_data=Admin(section="manage", action="fxqtime"))
    kb.button(text=t("fx_pn_qvol"), callback_data=Admin(section="manage", action="fxqvol"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(1)
    return kb.as_markup()


def fx_kind_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, kind in enumerate(PanelKind):
        kb.button(text=kind.value, callback_data=Admin(section="manage", action="fxkind", page=index))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanels"))
    kb.adjust(2)
    return kb.as_markup()


def _fx_leaves_text(t: Texts, panel: Panel, cfg: dict[str, Any]) -> str:
    options = panel.options if isinstance(panel.options, dict) else {}
    cap = capacity_of(options)
    move = options.get("move_price", cfg.get("move_price"))
    return t(
        "fx_leaves",
        renew=panel_renew_mode(options, cfg),
        capacity=cap if cap is not None else "-",
        agent=esc(str(options.get("agent_group") or "-")),
        move=money(move) if isinstance(move, int) else "-",
        limits="/".join(str(options.get(key, "-")) for key, _, _ in LIMIT_DEFS),
        hidden=len(hidden_ids(options)),
        national=t("fx_on") if options.get(NATIONAL_NET_OPTION) else t("fx_off"),
        emergency=t("fx_on") if options.get(EMERGENCY_OPTION) else t("fx_off"),
        iplimit=t("fx_on") if options.get("ip_limit_enabled") else t("fx_off"),
        test=t("fx_on") if trial_cfg(options)["enabled"] else t("fx_off"),
    )


def _fx_panel_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("mgmt_btn_check"), callback_data=Admin(section="manage", action="fxcheck", id=panel.id))
    key = "mgmt_btn_disable" if panel.is_active else "mgmt_btn_enable"
    kb.button(text=t(key), callback_data=Admin(section="manage", action="fxtoggle", id=panel.id))
    kb.button(text=t("fx_pd_addr"), callback_data=Admin(section="manage", action="fxaddr", id=panel.id))
    kb.button(text=t("fx_pd_key"), callback_data=Admin(section="manage", action="fxkey", id=panel.id))
    kb.button(
        text=t("mgmt_btn_rename"), callback_data=Admin(section="manage", action="fxrename", id=panel.id)
    )
    kb.button(
        text=t("mgmt_btn_extras"), callback_data=Admin(section="manage", action="fxextras", id=panel.id)
    )
    kb.button(
        text=t("mgmt_btn_umethod"), callback_data=Admin(section="manage", action="fxumethod", id=panel.id)
    )
    kb.button(text=t("fx_pd_renew"), callback_data=Admin(section="manage", action="fxrenew", id=panel.id))
    kb.button(text=t("fx_pd_capacity"), callback_data=Admin(section="manage", action="fxcap", id=panel.id))
    kb.button(text=t("fx_pd_agent"), callback_data=Admin(section="manage", action="fxagent", id=panel.id))
    kb.button(text=t("fx_pd_move"), callback_data=Admin(section="manage", action="fxmove", id=panel.id))
    kb.button(text=t("fx_pd_limits"), callback_data=Admin(section="manage", action="fxlimits", id=panel.id))
    kb.button(text=t("fx_pd_hidden"), callback_data=Admin(section="manage", action="fxhidden", id=panel.id))
    kb.button(text=t("fx_pd_nat"), callback_data=Admin(section="manage", action="fxnat", id=panel.id))
    kb.button(text=t("fx_pd_emg"), callback_data=Admin(section="manage", action="fxemg", id=panel.id))
    kb.button(text=t("fx_pd_ip"), callback_data=Admin(section="manage", action="fxip", id=panel.id))
    kb.button(text=t("fx_pd_fx"), callback_data=Admin(section="manage", action="fxdesk", id=panel.id))
    kb.button(text=t("fx_pd_test"), callback_data=Admin(section="manage", action="fxtest", id=panel.id))
    kb.button(text=t("mgmt_btn_flags"), callback_data=Admin(section="manage", action="fxflags", id=panel.id))
    kb.button(
        text=t("mgmt_btn_delete"), callback_data=Admin(section="manage", action="fxdelete", id=panel.id)
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpick"))
    kb.adjust(1)
    return kb.as_markup()


def _fx_extras_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("mgmt_btn_extra_gb"),
        callback_data=Admin(section="manage", action="fxextraprice", id=panel.id),
    )
    kb.button(
        text=t("mgmt_btn_extra_day"),
        callback_data=Admin(section="manage", action="fxextraprice", id=panel.id, page=1),
    )
    kb.button(
        text=t("mgmt_btn_sell_volume"), callback_data=Admin(section="manage", action="fxsell", id=panel.id)
    )
    kb.button(
        text=t("mgmt_btn_sell_days"),
        callback_data=Admin(section="manage", action="fxsell", id=panel.id, page=1),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(1)
    return kb.as_markup()


def _fx_flags_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    flags = showcase_flags(panel.options if isinstance(panel.options, dict) else {})
    kb = InlineKeyboardBuilder()
    for index, key in enumerate(SHOWCASE_KEYS):
        mark = "✅" if flags[key] else "❌"
        kb.button(
            text=f"{mark} {t(SHOWCASE_LABELS[key])}"[:64],
            callback_data=Admin(section="manage", action="fxflag", id=panel.id, page=index),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def _fx_hidden_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("fx_btn_hidden_add"),
        callback_data=Admin(section="manage", action="fxhiddenmode", id=panel.id),
    )
    kb.button(
        text=t("fx_btn_hidden_del"),
        callback_data=Admin(section="manage", action="fxhiddenmode", id=panel.id, page=1),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(2, 1)
    return kb.as_markup()


def _fx_limits_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, (option, _, _) in enumerate(LIMIT_DEFS):
        kb.button(
            text=option[:64],
            callback_data=Admin(section="manage", action="fxlimit", id=panel.id, page=index),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _fx_test_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("fx_btn_test_toggle"),
        callback_data=Admin(section="manage", action="fxtesttoggle", id=panel.id),
    )
    kb.button(
        text=t("fx_btn_test_reset"),
        callback_data=Admin(section="manage", action="fxtestreset", id=panel.id),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(1)
    return kb.as_markup()


def _fx_desk_kb(t: Texts, panel: Panel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("fx_btn_refresh"),
        callback_data=Admin(section="manage", action="fxdeskrefresh", id=panel.id),
    )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="fxpanel", id=panel.id))
    kb.adjust(1)
    return kb.as_markup()


def _fx_hidden_text(t: Texts, panel: Panel) -> str:
    options = panel.options if isinstance(panel.options, dict) else {}
    ids = hidden_ids(options)
    shown = ", ".join(str(tid) for tid in ids) if ids else t("fx_hidden_empty")
    return t("fx_hidden_title", list=shown)


# --- mirror hub and stats ------------------------------------------------------------


async def _fx_section(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    bot: Bot,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not _can_view(staff):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    if not 0 <= callback_data.page < len(HUB_SECTIONS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    name, key = HUB_SECTIONS[callback_data.page]
    if name == "stats":
        await _fx_show_stats(call, session, shop, t, bot, "today")
        return
    if name == "panels":
        await call.answer()
        await show(call, t("fx_panels_menu_title"), fx_panels_kb(t))
        return
    await _w2_section(call, name, session, shop, t, staff, cfg, state)


async def _fx_show_stats(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    bot: Bot,
    key: str,
) -> None:
    await call.answer()
    now = utcnow()
    if key == "all":
        since, until = ALL_SINCE, None
    else:
        since, until = pulse_window(key, now)
    pulse = await collect_pulse(
        session,
        shop.id,
        since,
        until,
        now=now,
        period=t(FX_SLICE_KEYS[key]),
        bot_ms=await _bot_latency(bot),
        db_ms=await _db_latency(session),
    )
    await show(call, full_pulse_text(t, pulse), fx_stats_kb(t, key))


async def _fx_close(call: CallbackQuery, t: Texts) -> None:
    message = call.message
    if isinstance(message, Message):
        try:
            await message.delete()
            await call.answer()
            return
        except Exception:
            pass
    await call.answer()
    await show(call, t("fx_hub_title", date=jdate(utcnow())), fx_hub_kb(t))


@router.message(ManageStates.range_start, F.text)
async def fx_range_start(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    del session, shop
    if not _can_view(staff):
        await state.clear()
        await message.answer(t("adm_forbidden"), reply_markup=menu(t, cfg, staff))
        return
    day = parse_custom_date(message.text)
    if day is None:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.set_state(ManageStates.range_end)
    await state.update_data(start=day.isoformat())
    await message.answer(t("fx_range_ask_end"))


@router.message(ManageStates.range_end, F.text)
async def fx_range_end(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    if not _can_view(staff):
        await state.clear()
        await message.answer(t("adm_forbidden"), reply_markup=menu(t, cfg, staff))
        return
    data = await state.get_data()
    try:
        start = datetime.fromisoformat(str(data.get("start") or ""))
    except ValueError:
        start = None
    end = parse_custom_date(message.text)
    if start is None or end is None or end < start:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.clear()
    now = utcnow()
    pulse = await collect_pulse(
        session,
        shop.id,
        start,
        end + timedelta(days=1),
        now=now,
        period=f"{jdate(start)} - {jdate(end)}",
        bot_ms=await _bot_latency(bot),
        db_ms=await _db_latency(session),
    )
    await message.answer(full_pulse_text(t, pulse), reply_markup=fx_stats_kb(t, "custom"))


# --- panel add wizard and quick prices -------------------------------------------------


@router.message(ManageStates.panel_add_name, F.text)
async def panel_add_name(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity, state: FSMContext
) -> None:
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 64:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.update_data(name=name)
    await state.set_state(ManageStates.panel_add_url)
    await message.answer(t("fx_add_url"), reply_markup=keyboards.cancel(t))


@router.message(ManageStates.panel_add_url, F.text)
async def panel_add_url(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity, state: FSMContext
) -> None:
    url = (message.text or "").strip()
    if len(url) > 255 or not url.startswith(("http://", "https://")):
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.update_data(url=url)
    await state.set_state(ManageStates.panel_add_user)
    await message.answer(t("fx_add_user"), reply_markup=keyboards.cancel(t))


@router.message(ManageStates.panel_add_user, F.text)
async def panel_add_user(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity, state: FSMContext
) -> None:
    username = (message.text or "").strip()
    if not 1 <= len(username) <= 128:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.update_data(username=username)
    await state.set_state(ManageStates.panel_add_secret)
    await message.answer(t("fx_add_secret"), reply_markup=keyboards.cancel(t))


@router.message(ManageStates.panel_add_secret, F.text)
async def panel_add_secret(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    secret = message.text or ""
    if not 1 <= len(secret) <= 255:
        await message.answer(t("mgmt_input_invalid"))
        return
    data = await state.get_data()
    name = str(data.get("name") or "")
    url = str(data.get("url") or "")
    username = str(data.get("username") or "")
    kind = str(data.get("kind") or "")
    if not name or not url or not username or kind not in {item.value for item in PanelKind}:
        await state.clear()
        await message.answer(t("button_expired"), reply_markup=menu(t, cfg, staff))
        return
    transient = Panel(
        shop_id=shop.id,
        name=name,
        kind=PanelKind(kind),
        base_url=url,
        username=username,
        password_enc=secretbox.encrypt("panel", secret),
        options={},
    )
    await message.answer(t("fx_add_testing"))
    try:
        driver = build_driver(transient)
        try:
            await driver.health_check()
        finally:
            await driver.close()
    except PanelError as exc:
        await state.clear()
        await message.answer(t("fx_add_fail", error=esc(str(exc)[:120])), reply_markup=menu(t, cfg, staff))
        return
    except ValueError as exc:
        # The SSRF guard raises ValueError-based UnsafeUrl, not PanelError.
        await state.clear()
        await message.answer(t("fx_add_fail", error=esc(str(exc)[:120])), reply_markup=menu(t, cfg, staff))
        return
    await state.clear()
    row = Panel(
        shop_id=shop.id,
        name=name,
        kind=transient.kind,
        base_url=url,
        username=username,
        password_enc=transient.password_enc,
        options={},
    )
    session.add(row)
    await session.flush()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.create",
        entity="panel",
        entity_id=row.id,
        after={"name": name, "kind": kind},
    )
    await session.commit()
    await message.answer(t("fx_add_ok", name=esc(name)), reply_markup=menu(t, cfg, staff))
    await message.answer(t("fx_panels_menu_title"), reply_markup=fx_panels_kb(t))


async def _quick_start(
    call: CallbackQuery, action: str, t: Texts, cfg: dict[str, Any], state: FSMContext
) -> None:
    kind = "time" if action == "fxqtime" else "volume"
    spec = QUICK_SPEC[kind]
    await call.answer()
    await state.update_data(kind=kind, values=[])
    await state.set_state(ManageStates.quick_price)
    if isinstance(call.message, Message):
        await call.message.answer(
            t(
                "fx_quick_ask",
                kind=t(f"fx_quick_{kind}"),
                step=1,
                current=quick_current(cfg.get(spec)),
            ),
            reply_markup=keyboards.cancel(t),
        )


@router.message(ManageStates.quick_price, F.text)
async def quick_price_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not staff.can("panels.write"):
        await state.clear()
        await message.answer(t("adm_forbidden"), reply_markup=menu(t, cfg, staff))
        return
    data = await state.get_data()
    kind = str(data.get("kind") or "time")
    spec = QUICK_SPEC.get(kind, QUICK_SPEC["time"])
    values = [int(item) for item in (data.get("values") or [])]
    value = parse_quick_price(message.text)
    if value is None:
        await message.answer(t("mgmt_input_invalid"))
        return
    values.append(value)
    if len(values) < 3:
        await state.update_data(values=values)
        await message.answer(
            t(
                "fx_quick_ask",
                kind=t(f"fx_quick_{kind}"),
                step=len(values) + 1,
                current=quick_current(cfg.get(spec)),
            )
        )
        return
    await state.clear()
    payload = {"tier1": values[0], "tier2": values[1], "tier3": values[2]}
    await shop_settings.set_many(session, shop.id, {spec: payload})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.quick_prices",
        entity="shop",
        entity_id=shop.id,
        after={spec: payload},
    )
    await session.commit()
    await message.answer(t("fx_quick_saved"), reply_markup=menu(t, cfg, staff))


# --- mirror panel leaves ---------------------------------------------------------------


def _fx_amount(value: int) -> str:
    return money(value) if value > 0 else "-"


async def _fx_desk(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    cfg: dict[str, Any],
    refresh: bool,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    if refresh:
        try:
            await fx_rates.refresh()
        except Exception:
            pass
    try:
        live = await fx_rates.cached()
    except Exception:
        live = {}
    markup = max(0, int(cfg.get("fx_markup_percent") or 0))
    manual = {symbol: max(0, int(cfg.get(f"{symbol}_rate") or 0)) for symbol in ("usdt", "trx", "ton")}
    effective = {
        symbol: (fx_effective(int(live.get(symbol) or 0) or manual[symbol], markup)) for symbol in manual
    }
    await call.answer()
    await show(
        call,
        t(
            "fx_desk_title",
            enabled=t("fx_on") if cfg.get("crypto_enabled") else t("fx_off"),
            source=esc(str(cfg.get("fx_source") or "-")),
            usdt=_fx_amount(manual["usdt"]),
            trx=_fx_amount(manual["trx"]),
            ton=_fx_amount(manual["ton"]),
            live_usdt=_fx_amount(effective["usdt"]),
            live_trx=_fx_amount(effective["trx"]),
            live_ton=_fx_amount(effective["ton"]),
            markup=markup,
        ),
        _fx_desk_kb(t, panel),
    )


async def _fx_test(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    action: str,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    options = dict(panel.options if isinstance(panel.options, dict) else {})
    if action in ("fxtesttoggle", "fxtestreset"):
        if action == "fxtesttoggle":
            raw = dict(options.get("test_cfg") or {})
            raw["enabled"] = not trial_cfg(options)["enabled"]
            options["test_cfg"] = raw
        else:
            options["test_cfg"] = dict(TEST_CFG_DEFAULTS)
        panel.options = options
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.test",
            entity="panel",
            entity_id=panel.id,
            after={"test_cfg": options["test_cfg"]},
        )
        await session.commit()
    current = trial_cfg(panel.options if isinstance(panel.options, dict) else {})
    await call.answer(t("adm_saved") if action != "fxtest" else None)
    await show(
        call,
        t(
            "fx_test_title",
            enabled=t("fx_on") if current["enabled"] else t("fx_off"),
            days=current["days"],
            gb=current["gb"],
            per_user=current["max_per_user"],
        ),
        _fx_test_kb(t, panel),
    )


async def _fx_toggle(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    action: str,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    options = dict(panel.options if isinstance(panel.options, dict) else {})
    if action == "fxrenew":
        effective = panel_renew_mode(options, cfg) == "extend_reset"
        options["renew_resets_usage"] = not effective
        option, value = "renew_resets_usage", options["renew_resets_usage"]
    else:
        # Plain boolean panel options. NATIONAL_NET_OPTION and EMERGENCY_OPTION change how
        # the registry classifies the panel; ip_limit_enabled is a boolean-only flag for
        # now (no driver reads it yet) so later work can wire per-panel device limits.
        option = {"fxnat": NATIONAL_NET_OPTION, "fxemg": EMERGENCY_OPTION}[action]
        if action == "fxip":
            option = "ip_limit_enabled"
        value = not bool(options.get(option))
        options[option] = value
    panel.options = options
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.option",
        entity="panel",
        entity_id=panel.id,
        after={option: value},
    )
    await session.commit()
    await drop_driver(panel.id)
    await call.answer(t("adm_saved"))
    await _panel(
        call, Admin(section="manage", action="fxpanel", id=panel.id), session, shop, t, staff, cfg, True
    )


async def _fx_value_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    cfg: dict[str, Any],
    action: str,
    state: FSMContext,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    options = panel.options if isinstance(panel.options, dict) else {}
    if action == "fxcap":
        option, vkind, current, lo, hi = ("capacity_limit", "cap", capacity_of(options), 0, 1000000)
        shown = str(current) if current is not None else "-"
    elif action == "fxmove":
        move = options.get("move_price", cfg.get("move_price"))
        option, vkind, lo, hi = ("move_price", "price", 0, 100000000)
        shown = money(move) if isinstance(move, int) else "-"
    elif action == "fxaddr":
        option, vkind, shown, lo, hi = ("base_url", "url", panel.base_url, 0, 0)
    else:
        option, vkind, lo, hi = ("agent_group", "str", 0, 0)
        shown = str(options.get("agent_group") or "-")
    await call.answer()
    await state.update_data(panel_id=panel.id, option=option, vkind=vkind, lo=lo, hi=hi)
    await state.set_state(ManageStates.panel_value)
    if isinstance(call.message, Message):
        await call.message.answer(
            t("fx_value_ask", name=t(VALUE_LABELS[action]), current=shown),
            reply_markup=keyboards.cancel(t),
        )


@router.message(ManageStates.panel_value, F.text)
async def panel_value_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    panel = await _scoped_panel(session, shop, int(data.get("panel_id") or 0))
    if panel is None or not staff.can("panels.write"):
        await state.clear()
        key = "mgmt_not_found" if panel is None else "adm_forbidden"
        await message.answer(t(key), reply_markup=menu(t, cfg, staff))
        return
    option = str(data.get("option") or "")
    vkind = str(data.get("vkind") or "str")
    raw = (message.text or "").strip()
    value: Any = None
    remove = False
    if option == "base_url":
        if len(raw) > 255 or not raw.startswith(("http://", "https://")):
            await message.answer(t("mgmt_input_invalid"))
            return
        before_url = panel.base_url
        panel.base_url = raw
        await state.clear()
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.option",
            entity="panel",
            entity_id=panel.id,
            before={"base_url": before_url},
            after={"base_url": raw},
        )
        await session.commit()
        await drop_driver(panel.id)
        await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
        await message.answer(_fx_leaves_text(t, panel, cfg), reply_markup=_fx_panel_kb(t, panel))
        return
    if vkind == "price":
        parsed = parse_quick_price(raw)
        if parsed is None:
            await message.answer(t("mgmt_input_invalid"))
            return
        value, remove = parsed, parsed == 0
    elif vkind == "cap":
        parsed = parse_int(raw)
        if parsed is None or not 0 <= parsed <= 1000000:
            await message.answer(t("mgmt_input_invalid"))
            return
        value, remove = parsed, parsed == 0
    elif vkind == "int":
        parsed = parse_int(raw)
        lo, hi = int(data.get("lo") or 0), int(data.get("hi") or 0)
        if parsed is None or not lo <= parsed <= hi:
            await message.answer(t("mgmt_input_invalid"))
            return
        value = parsed
    elif raw == "-":
        remove = True
    elif 1 <= len(raw) <= 64:
        value = raw
    else:
        await message.answer(t("mgmt_input_invalid"))
        return
    options = dict(panel.options if isinstance(panel.options, dict) else {})
    before = options.get(option)
    if remove:
        options.pop(option, None)
    else:
        options[option] = value
    panel.options = options
    await state.clear()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.option",
        entity="panel",
        entity_id=panel.id,
        before={option: before},
        after={option: None if remove else value},
    )
    await session.commit()
    await drop_driver(panel.id)
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(_fx_leaves_text(t, panel, cfg), reply_markup=_fx_panel_kb(t, panel))


async def _fx_limits(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    options = panel.options if isinstance(panel.options, dict) else {}
    await call.answer()
    await show(
        call,
        t(
            "fx_limits_title",
            min_gb=options.get("min_custom_gb", "-"),
            max_gb=options.get("max_custom_gb", "-"),
            min_days=options.get("min_custom_days", "-"),
            max_days=options.get("max_custom_days", "-"),
        ),
        _fx_limits_kb(t, panel),
    )


async def _fx_limit_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    if not 0 <= callback_data.page < len(LIMIT_DEFS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    option, lo, hi = LIMIT_DEFS[callback_data.page]
    options = panel.options if isinstance(panel.options, dict) else {}
    await call.answer()
    await state.update_data(panel_id=panel.id, option=option, vkind="int", lo=lo, hi=hi)
    await state.set_state(ManageStates.panel_value)
    if isinstance(call.message, Message):
        await call.message.answer(
            t("fx_value_ask", name=option, current=options.get(option, "-")),
            reply_markup=keyboards.cancel(t),
        )


async def _fx_hidden_show(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    await call.answer()
    await show(call, _fx_hidden_text(t, panel), _fx_hidden_kb(t, panel))


async def _fx_hidden_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    if callback_data.page not in (0, 1):
        await call.answer(t("button_expired"), show_alert=True)
        return
    await call.answer()
    await state.update_data(panel_id=panel.id, mode="remove" if callback_data.page else "add")
    await state.set_state(ManageStates.panel_hidden)
    if isinstance(call.message, Message):
        await call.message.answer(t("fx_hidden_ask"), reply_markup=keyboards.cancel(t))


@router.message(ManageStates.panel_hidden, F.text)
async def panel_hidden_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    panel = await _scoped_panel(session, shop, int(data.get("panel_id") or 0))
    if panel is None or not staff.can("panels.write"):
        await state.clear()
        key = "mgmt_not_found" if panel is None else "adm_forbidden"
        await message.answer(t(key), reply_markup=menu(t, cfg, staff))
        return
    tid = parse_int(message.text)
    if tid is None or tid <= 0:
        await message.answer(t("mgmt_input_invalid"))
        return
    options = panel.options if isinstance(panel.options, dict) else {}
    before = hidden_ids(options)
    if str(data.get("mode") or "add") == "remove":
        panel.options = with_hidden_removed(options, tid)
    else:
        panel.options = with_hidden_added(options, tid)
    await state.clear()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.hidden",
        entity="panel",
        entity_id=panel.id,
        before={"hide_user": before},
        after={"hide_user": hidden_ids(panel.options)},
    )
    await session.commit()
    await drop_driver(panel.id)
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(_fx_hidden_text(t, panel), reply_markup=_fx_hidden_kb(t, panel))


async def _fx_key_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    panel = await _scoped_panel(session, shop, callback_data.id)
    if panel is None:
        await call.answer(t("mgmt_not_found"), show_alert=True)
        return
    options = panel.options if isinstance(panel.options, dict) else {}
    # Token-auth panels (mirza/nexora-style setups) keep the token in options["api_key"];
    # every native kind authenticates with the username + password pair instead.
    target = "api_key" if "api_key" in options else "password"
    await call.answer()
    await state.update_data(panel_id=panel.id, target=target)
    await state.set_state(ManageStates.panel_secret)
    if isinstance(call.message, Message):
        await call.message.answer(t("fx_secret_ask"), reply_markup=keyboards.cancel(t))


@router.message(ManageStates.panel_secret, F.text)
async def panel_secret_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    panel = await _scoped_panel(session, shop, int(data.get("panel_id") or 0))
    if panel is None or not staff.can("panels.write"):
        await state.clear()
        key = "mgmt_not_found" if panel is None else "adm_forbidden"
        await message.answer(t(key), reply_markup=menu(t, cfg, staff))
        return
    secret = message.text or ""
    if not 1 <= len(secret) <= 255:
        await message.answer(t("mgmt_input_invalid"))
        return
    await state.clear()
    if str(data.get("target") or "password") == "api_key":
        options = dict(panel.options if isinstance(panel.options, dict) else {})
        options["api_key"] = secret
        panel.options = options
        target = "api_key"
    else:
        panel.password_enc = secretbox.encrypt("panel", secret)
        target = "password"
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.rekey",
        entity="panel",
        entity_id=panel.id,
        after={"target": target},
    )
    await session.commit()
    await drop_driver(panel.id)
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(_fx_leaves_text(t, panel, cfg), reply_markup=_fx_panel_kb(t, panel))


# --- wave 2: finance / shop / users / support / help / channels / tech --------
# Native reimplementation of the remaining Faoxima management sections (MGMT-20..26):
# the same menu shape and Persian labels, mapped onto our backend. Every mutation is
# shop-scoped, audited and answered; reads degrade gracefully when data is missing.

W2_GATEWAYS: tuple[tuple[str, str, str | None, str | None], ...] = (
    # (gateway key, enabled spec, min-amount spec, max-amount spec)
    ("card", "card_enabled", "card_min_amount", "card_max_amount"),
    ("stars", "stars_enabled", None, None),
    ("crypto", "crypto_enabled", None, None),
    ("zarinpal", "zarinpal_enabled", None, None),
    ("nowpayments", "nowpayments_enabled", None, None),
    ("plisio", "plisio_enabled", None, None),
    ("aqayepardakht", "aqayepardakht_enabled", None, None),
    ("tetra98", "tetra98_enabled", None, None),
    ("variza", "variza_enabled", None, None),
    ("abangateway", "abangateway_enabled", None, None),
    ("tronado", "tronado_enabled", None, None),
    ("tonpay", "tonpay_enabled", None, None),
    ("cubepay", "cubepay_enabled", None, None),
    ("atlaspay", "atlaspay_enabled", None, None),
    ("blupal", "blupal_enabled", None, None),
)

W2_CARD_BOOL_FIELDS: tuple[tuple[str, str], ...] = (
    ("card_enabled", "w2_card_enabled"),
    ("card_pv_mode", "w2_card_pv_mode"),
    ("card_first_pay_only", "w2_card_first_pay_only"),
    ("card_auto_approve", "w2_card_auto_approve"),
    ("card_random_tail", "w2_card_random_tail"),
    ("card_verify_enabled", "w2_card_verify_enabled"),
)

W2_CARD_INT_FIELDS: tuple[tuple[str, str, int, int], ...] = (
    ("card_auto_max_amount", "w2_card_auto_max_amount", 0, 100_000_000),
    ("card_auto_min_history", "w2_card_auto_min_history", 0, 1000),
    ("card_auto_minutes", "w2_card_auto_minutes", 0, 100_000),
    ("card_verify_min_amount", "w2_card_verify_min_amount", 0, 100_000_000),
    ("card_min_amount", "w2_card_min_amount", 0, 100_000_000),
    ("card_max_amount", "w2_card_max_amount", 0, 100_000_000),
    ("card_cashback_percent", "w2_card_cashback_percent", 0, 100),
)

W2_TECH_TOGGLES: tuple[tuple[str, str], ...] = (
    ("bot_enabled", "w2_tech_bot"),
    ("trial_enabled", "w2_tech_trial"),
    ("guest_buy_enabled", "w2_tech_guest"),
    ("business_enabled", "w2_tech_business"),
    ("wheel_enabled", "w2_tech_wheel"),
    ("lottery_enabled", "w2_tech_lottery"),
    ("dice_enabled", "w2_tech_dice"),
    ("bulk_buy_enabled", "w2_tech_bulk"),
    ("trial_requires_phone", "w2_tech_phone"),
)

W2_HELP_KEYS: tuple[str, ...] = ("welcome", "help")


def w2_on(value: Any) -> str:
    """A toggle mark for a truthy shop value."""
    return "✅" if value else "❌"


def w2_support_departments(cfg: dict[str, Any]) -> list[str]:
    """Support department names from the `support_departments` setting; junk ignored."""
    raw = cfg.get("support_departments")
    if not isinstance(raw, list):
        return []
    return [str(name)[:48] for name in raw if str(name).strip()][:20]


def w2_with_department(cfg: dict[str, Any], name: str) -> list[str]:
    """Department list with `name` added (deduped, capped)."""
    out = w2_support_departments(cfg)
    clean = name.strip()[:48]
    if clean and clean not in out and len(out) < 20:
        out.append(clean)
    return out


def w2_without_department(cfg: dict[str, Any], name: str) -> list[str]:
    """Department list without `name`."""
    return [item for item in w2_support_departments(cfg) if item != name]


def w2_card_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Shop card rows for card-to-card; each is {number, owner}."""
    raw = cfg.get("card_accounts")
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        number = "".join(ch for ch in str(row.get("number", "")) if ch.isdigit())
        if len(number) < 12:
            continue
        out.append({"number": number, "owner": str(row.get("owner", ""))[:48]})
    return out[:20]


def w2_card_without(accounts: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """Card rows without the row at `index`."""
    return [row for pos, row in enumerate(accounts) if pos != index]


def w2_secret_present(cfg: dict[str, Any], key: str) -> bool:
    """Whether a secret-typed shop value is set (presence only, never the value)."""
    return bool(cfg.get(key))


async def _w2_set_bool(
    session: AsyncSession,
    shop: ShopRow,
    staff: StaffIdentity,
    key: str,
    action_name: str,
    entity: str = "shop",
) -> bool:
    """Flip one boolean shop setting, audit it and return the new value."""
    cfg = await shop_settings.load(session, shop.id)
    value = not bool(cfg.get(key))
    await shop_settings.set_many(session, shop.id, {key: value})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=action_name,
        entity=entity,
        entity_id=shop.id,
        after={key: value},
    )
    await session.commit()
    return value


async def _w2_ask(
    call: CallbackQuery,
    t: Texts,
    state: FSMContext,
    *,
    form: str,
    prompt_key: str,
    back_action: str,
    back_id: int = 0,
    back_page: int = 0,
    extra: dict[str, Any] | None = None,
    **prompt_args: Any,
) -> None:
    """Open the generic wave-2 value form and remember where to return."""
    await call.answer()
    await state.set_state(ManageStates.w2_value)
    data: dict[str, Any] = {
        "w2form": form,
        "back_action": back_action,
        "back_id": back_id,
        "back_page": back_page,
    }
    if extra:
        data.update(extra)
    await state.update_data(**data)
    if isinstance(call.message, Message):
        await call.message.answer(t(prompt_key, **prompt_args), reply_markup=keyboards.cancel(t))


def _w2_back(data: dict[str, Any]) -> Admin:
    return Admin(
        section="manage",
        action=str(data.get("back_action") or "hub"),
        id=int(data.get("back_id") or 0),
        page=int(data.get("back_page") or 0),
    )


async def _w2_rerender(
    call: CallbackQuery,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    """After a wave-2 form submit, go back to the remembered screen."""
    await state.clear()
    await deck(call, _w2_back(data), session, shop, t, staff, cfg, state, bot)


# --- wave 2 screens: finance ----------------------------------------------------


def _w2_fin_kb(t: Texts, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, (key, enabled, _min, _max) in enumerate(W2_GATEWAYS):
        mark = w2_on(cfg.get(enabled))
        kb.button(
            text=f"{t(f'w2_gw_{key}')[:20]} {mark}"[:64],
            callback_data=Admin(section="manage", action="w2gw", id=index),
        )
    kb.button(text=t("w2_topup_limits"), callback_data=Admin(section="manage", action="w2topup"))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="manage", action="hub")))
    return kb.as_markup()


def _w2_gw_text(t: Texts, index: int, cfg: dict[str, Any]) -> str:
    key, enabled, min_spec, max_spec = W2_GATEWAYS[index]
    lines = [t("w2_gw_title", name=t(f"w2_gw_{key}"), state=w2_on(cfg.get(enabled)))]
    if min_spec is not None:
        lines.append(t("w2_gw_min", value=money(int(cfg.get(min_spec) or 0))))
    if max_spec is not None:
        lines.append(t("w2_gw_max", value=money(int(cfg.get(max_spec) or 0))))
    if key == "card":
        lines.append(t("w2_gw_cards", count=len(w2_card_accounts(cfg))))
    return "\n".join(lines)


def _w2_gw_kb(t: Texts, index: int, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    key, enabled, min_spec, max_spec = W2_GATEWAYS[index]
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"{t('w2_toggle')} {w2_on(cfg.get(enabled))}"[:64],
        callback_data=Admin(section="manage", action="w2gwt", id=index),
    )
    if key == "card":
        kb.button(text=t("w2_card_desk"), callback_data=Admin(section="manage", action="w2card"))
    if min_spec is not None:
        kb.button(
            text=t("w2_gw_set_min")[:64],
            callback_data=Admin(section="manage", action="w2gwmin", id=index),
        )
    if max_spec is not None:
        kb.button(
            text=t("w2_gw_set_max")[:64],
            callback_data=Admin(section="manage", action="w2gwmax", id=index),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2fin"))
    kb.adjust(1)
    return kb.as_markup()


def _w2_card_text(t: Texts, cfg: dict[str, Any]) -> str:
    lines = [t("w2_card_title", name=cfg.get("card_title") or t("w2_card_default_name"))]
    for spec, label in W2_CARD_BOOL_FIELDS:
        lines.append(t("w2_card_row", name=t(label), state=w2_on(cfg.get(spec))))
    for spec, label, _lo, _hi in W2_CARD_INT_FIELDS:
        lines.append(t("w2_card_amount_row", name=t(label), value=money(int(cfg.get(spec) or 0))))
    lines.append(t("w2_card_accounts", count=len(w2_card_accounts(cfg))))
    lines.append(t("w2_card_support", username=cfg.get("card_pv_username") or "-"))
    return "\n".join(lines)


def _w2_card_kb(t: Texts, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pos, (spec, label) in enumerate(W2_CARD_BOOL_FIELDS):
        kb.button(
            text=f"{t(label)[:24]} {w2_on(cfg.get(spec))}"[:64],
            callback_data=Admin(section="manage", action="w2cardt", id=pos),
        )
    kb.button(text=t("w2_card_edit_amounts"), callback_data=Admin(section="manage", action="w2cardi"))
    kb.button(text=t("w2_card_edit_title"), callback_data=Admin(section="manage", action="w2cardtitle"))
    kb.button(text=t("w2_card_edit_support"), callback_data=Admin(section="manage", action="w2cardsup"))
    kb.button(text=t("w2_card_edit_help"), callback_data=Admin(section="manage", action="w2cardhelp"))
    kb.button(text=t("w2_card_accounts_btn"), callback_data=Admin(section="manage", action="w2cardacc"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2fin"))
    kb.adjust(2, 2, 2, 1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def _w2_card_acc_kb(t: Texts, accounts: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pos, row in enumerate(accounts):
        kb.button(
            text=f"❌ {masked(row['number'])}"[:64],
            callback_data=Admin(section="manage", action="w2carddel", id=pos),
        )
    kb.button(text=t("w2_card_add"), callback_data=Admin(section="manage", action="w2cardadd"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2card"))
    kb.adjust(1)
    return kb.as_markup()


async def _w2_finance(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not (staff.can("dashboard.view") or staff.can("payments.read")):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id)
    await show(call, t("w2_finance_title"), _w2_fin_kb(t, cfg))


async def _w2_gateway(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    if not 0 <= callback_data.id < len(W2_GATEWAYS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id)
    await show(call, _w2_gw_text(t, callback_data.id, cfg), _w2_gw_kb(t, callback_data.id, cfg))


async def _w2_gateway_toggle(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    if not staff.can("settings.write"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    if not 0 <= callback_data.id < len(W2_GATEWAYS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    _key, enabled, _min, _max = W2_GATEWAYS[callback_data.id]
    await _w2_set_bool(session, shop, staff, enabled, "settings.gateway_toggle")
    await call.answer(t("adm_saved"))
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    await show(call, _w2_gw_text(t, callback_data.id, cfg), _w2_gw_kb(t, callback_data.id, cfg))


async def _w2_card_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not (staff.can("dashboard.view") or staff.can("payments.read")):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id)
    await show(call, _w2_card_text(t, cfg), _w2_card_kb(t, cfg))


async def _w2_card_toggle(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    if not staff.can("settings.write"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    if not 0 <= callback_data.id < len(W2_CARD_BOOL_FIELDS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    spec = W2_CARD_BOOL_FIELDS[callback_data.id][0]
    await _w2_set_bool(session, shop, staff, spec, "settings.card_toggle")
    await call.answer(t("adm_saved"))
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    await show(call, _w2_card_text(t, cfg), _w2_card_kb(t, cfg))


# --- wave 2 screens: shop / users / support / help / channels / tech --------------


def _w2_shop_kb(t: Texts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("w2_shop_cats"), callback_data=Admin(section="manage", action="w2cats"))
    kb.button(text=t("w2_shop_plans"), callback_data=Admin(section="manage", action="w2plans"))
    kb.button(text=t("w2_shop_gifts"), callback_data=Admin(section="manage", action="w2gifts"))
    kb.button(text=t("w2_shop_discounts"), callback_data=Admin(section="manage", action="w2discs"))
    kb.button(text=t("w2_shop_bulk"), callback_data=Admin(section="manage", action="w2bulk"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


async def _w2_shop_show(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    cfg = await shop_settings.load(session, shop.id)
    cats = int(await session.scalar(select(func.count(Category.id)).where(Category.shop_id == shop.id)) or 0)
    plans = int(await session.scalar(select(func.count(Plan.id)).where(Plan.shop_id == shop.id)) or 0)
    gifts = int(
        await session.scalar(
            select(func.count(GiftCode.id)).where(GiftCode.shop_id == shop.id, GiftCode.is_active.is_(True))
        )
        or 0
    )
    codes = int(
        await session.scalar(
            select(func.count(DiscountCode.id)).where(
                DiscountCode.shop_id == shop.id, DiscountCode.is_active.is_(True)
            )
        )
        or 0
    )
    text = t(
        "w2_shop_title",
        cats=cats,
        plans=plans,
        gifts=gifts,
        codes=codes,
        bulk_max=int(cfg.get("bulk_buy_max") or 0),
        renew_cb=int(cfg.get("renew_cashback_percent") or 0),
    )
    await call.answer()
    await show(call, text, _w2_shop_kb(t))


async def _w2_users_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not staff.can("customers.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    members = list(
        await session.scalars(select(Staff).where(Staff.shop_id == shop.id).order_by(Staff.id).limit(30))
    )
    lines = [t("w2_users_title")]
    for row in members:
        mark = "✅" if row.is_active else "⛔"
        who = esc(row.display_name or row.username or str(row.telegram_id or row.id))
        lines.append(t("w2_users_row", mark=mark, who=who, role=row.role.value))
    kb = InlineKeyboardBuilder()
    kb.button(text=t("adm_lookup"), callback_data=Admin(section="lookup"))
    kb.button(text=t("adm_payments"), callback_data=Admin(section="payments"))
    if staff.can("staff.manage"):
        kb.button(text=t("w2_staff_add"), callback_data=Admin(section="manage", action="w2staffadd"))
        for row in members:
            if row.is_active and (staff.id is None or row.id != staff.id):
                kb.button(
                    text=t("w2_staff_del", who=(row.display_name or row.username or row.id))[:64],
                    callback_data=Admin(section="manage", action="w2staffdel", id=row.id),
                )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(2, 1)
    await show(call, "\n".join(lines), kb.as_markup())


async def _w2_support_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not staff.can("tickets.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id)
    depts = w2_support_departments(cfg)
    contacts = list(
        await session.scalars(
            select(SupportContact).where(SupportContact.shop_id == shop.id).order_by(SupportContact.sort)
        )
    )
    opened = int(
        await session.scalar(
            select(func.count(Ticket.id)).where(Ticket.shop_id == shop.id, Ticket.status == TicketStatus.OPEN)
        )
        or 0
    )
    lines = [
        t("w2_support_title", username=cfg.get("support_username") or "-", opened=opened),
        t("w2_support_depts", items=", ".join(depts) if depts else "-"),
    ]
    for row in contacts:
        lines.append(t("w2_support_contact", title=esc(row.title), url=esc(row.url)))
    kb = InlineKeyboardBuilder()
    if staff.can("settings.write"):
        kb.button(text=t("w2_sup_user"), callback_data=Admin(section="manage", action="w2supuser"))
        kb.button(text=t("w2_dept_add"), callback_data=Admin(section="manage", action="w2deptadd"))
        kb.button(text=t("w2_contact_add"), callback_data=Admin(section="manage", action="w2contactadd"))
        for pos, name in enumerate(depts):
            kb.button(
                text=t("w2_dept_del", name=name)[:64],
                callback_data=Admin(section="manage", action="w2deptdel", id=pos),
            )
        for row in contacts:
            kb.button(
                text=t("w2_contact_del", title=row.title)[:64],
                callback_data=Admin(section="manage", action="w2contactdel", id=row.id),
            )
    kb.button(text=t("adm_tickets"), callback_data=Admin(section="tickets"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(2, 1)
    await show(call, "\n".join(lines), kb.as_markup())


async def _w2_help_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not staff.can("settings.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    rows = list(
        await session.scalars(
            select(BotText)
            .where(BotText.shop_id == shop.id, BotText.locale == "fa")
            .order_by(BotText.key)
            .limit(40)
        )
    )
    from paurubot.i18n import catalog as _catalog

    known = set(_catalog("fa"))
    lines = [t("w2_help_title", count=len(rows))]
    kb = InlineKeyboardBuilder()
    for row in rows:
        lines.append(t("w2_help_row", key=esc(row.key)))
        if staff.can("settings.write"):
            kb.button(
                text=t("w2_help_edit", key=row.key)[:64],
                callback_data=Admin(section="manage", action="w2helpedit", id=row.id),
            )
            kb.button(
                text=t("w2_help_del", key=row.key)[:64],
                callback_data=Admin(section="manage", action="w2helpdel", id=row.id),
            )
    if staff.can("settings.write"):
        for pos, key in enumerate(W2_HELP_KEYS):
            if key in known:
                kb.button(
                    text=t("w2_help_new", key=key)[:64],
                    callback_data=Admin(section="manage", action="w2helpnew", id=pos),
                )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


async def _w2_channels_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not staff.can("settings.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    forced = list(
        await session.scalars(
            select(ForcedChannel).where(ForcedChannel.shop_id == shop.id).order_by(ForcedChannel.sort)
        )
    )
    logged = list(
        await session.scalars(
            select(LogChannel).where(LogChannel.shop_id == shop.id).order_by(LogChannel.topic)
        )
    )
    lines = [t("w2_channels_title", count=len(forced))]
    for row in forced:
        lines.append(t("w2_channels_row", title=esc(row.title), chat=esc(row.chat_id)))
    if logged:
        lines.append(t("w2_log_title"))
        for row in logged:
            lines.append(t("w2_log_row", topic=esc(row.topic), chat=row.chat_id))
    kb = InlineKeyboardBuilder()
    if staff.can("settings.write"):
        kb.button(text=t("w2_ch_add"), callback_data=Admin(section="manage", action="w2chadd"))
        for row in forced:
            kb.button(
                text=t("w2_ch_del", title=row.title)[:64],
                callback_data=Admin(section="manage", action="w2chdel", id=row.id),
            )
        kb.button(text=t("w2_log_set"), callback_data=Admin(section="manage", action="w2logset"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


async def _w2_tech_show(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity
) -> None:
    if not staff.can("settings.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    cfg = await shop_settings.load(session, shop.id)
    from paurubot import __version__, kv

    try:
        pong = await kv.get_redis().ping()
    except Exception:
        pong = False
    kb = InlineKeyboardBuilder()
    for pos, (key, label) in enumerate(W2_TECH_TOGGLES):
        mark = w2_on(cfg.get(key))
        if staff.can("settings.write"):
            kb.button(
                text=f"{t(label)[:24]} {mark}"[:64],
                callback_data=Admin(section="manage", action="w2techt", id=pos),
            )
        else:
            kb.button(
                text=f"{t(label)[:24]} {mark}"[:64], callback_data=Admin(section="manage", action="w2tech")
            )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="hub"))
    kb.adjust(2, 2, 2, 2, 1, 1)
    await show(
        call,
        t("w2_tech_title", version=__version__, redis=t("w2_on") if pong else t("w2_off")),
        kb.as_markup(),
    )


# --- wave 2 dispatch + forms --------------------------------------------------------


async def _w2_section(
    call: CallbackQuery,
    name: str,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not _can_view(staff):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await call.answer()
    if name == "shop":
        await _w2_shop_show(call, session, shop, t)
    elif name == "finance":
        await _w2_finance(call, session, shop, t, staff)
    elif name == "users":
        await _w2_users_show(call, session, shop, t, staff)
    elif name == "support":
        await _w2_support_show(call, session, shop, t, staff)
    elif name == "help":
        await _w2_help_show(call, session, shop, t, staff)
    elif name == "channel":
        await _w2_channels_show(call, session, shop, t, staff)
    elif name == "tech":
        await _w2_tech_show(call, session, shop, t, staff)
    else:
        await show(
            call, t("fx_stub", name=name), keyboards.back(t, Admin(section="manage", action="hub").pack())
        )


async def _w2_need(staff: StaffIdentity, call: CallbackQuery, t: Texts, perm: str) -> bool:
    """True when `staff` may mutate; otherwise answers forbidden."""
    if staff.can(perm):
        return True
    await call.answer(t("adm_forbidden"), show_alert=True)
    return False


async def _w2_dispatch(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    action = callback_data.action
    if action == "w2fin":
        await _w2_finance(call, session, shop, t, staff)
        return
    if action == "w2gw":
        await _w2_gateway(call, callback_data, session, shop, t)
        return
    if action == "w2gwt":
        await _w2_gateway_toggle(call, callback_data, session, shop, t, staff)
        return
    if action in ("w2gwmin", "w2gwmax"):
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        _key, _enabled, min_spec, max_spec = W2_GATEWAYS[callback_data.id]
        spec = min_spec if action == "w2gwmin" else max_spec
        if spec is None:
            await call.answer(t("button_expired"), show_alert=True)
            return
        await _w2_ask(
            call,
            t,
            state,
            form="intspec",
            prompt_key="w2_limit_ask",
            back_action="w2gw",
            back_id=callback_data.id,
            extra={"spec": spec, "min": 0, "max": 1_000_000_000, "audit": "settings.gateway_limit"},
        )
        return
    if action == "w2topup":
        await call.answer()
        await show(
            call,
            t(
                "w2_topup_title",
                low=money(int(cfg.get("min_topup") or 0)),
                high=money(int(cfg.get("max_topup") or 0)),
            ),
            keyboards.back(t, Admin(section="manage", action="w2fin").pack()),
        )
        return
    if action == "w2card":
        await _w2_card_show(call, session, shop, t, staff)
        return
    if action == "w2cardt":
        await _w2_card_toggle(call, callback_data, session, shop, t, staff)
        return
    if action == "w2cardi":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await call.answer()
        kb = InlineKeyboardBuilder()
        for pos, (_spec, label) in enumerate(W2_CARD_INT_FIELDS):
            kb.button(
                text=t(label)[:64],
                callback_data=Admin(section="manage", action="w2cardf", id=pos),
            )
        kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2card"))
        kb.adjust(1)
        await show(call, t("w2_card_pick_amount"), kb.as_markup())
        return
    if action == "w2cardf":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        if not 0 <= callback_data.id < len(W2_CARD_INT_FIELDS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        spec, _label, lo, hi = W2_CARD_INT_FIELDS[callback_data.id]
        await _w2_ask(
            call,
            t,
            state,
            form="intspec",
            prompt_key="w2_int_ask",
            back_action="w2card",
            extra={"spec": spec, "min": lo, "max": hi, "audit": "settings.card_amount"},
        )
        return
    if action in ("w2cardtitle", "w2cardsup", "w2cardhelp"):
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        spec = {"w2cardtitle": "card_title", "w2cardsup": "card_pv_username", "w2cardhelp": "card_help"}[
            action
        ]
        await _w2_ask(
            call,
            t,
            state,
            form="strspec",
            prompt_key="w2_str_ask",
            back_action="w2card",
            extra={"spec": spec, "maxlen": 255, "audit": "settings.card_text"},
        )
        return
    if action == "w2cardacc":
        if not (staff.can("dashboard.view") or staff.can("payments.read")):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await call.answer()
        cfg = await shop_settings.load(session, shop.id)
        accounts = w2_card_accounts(cfg)
        lines = [t("w2_card_acc_title", count=len(accounts))]
        for row in accounts:
            lines.append(t("w2_card_acc_row", card=masked(row["number"]), owner=esc(row["owner"] or "-")))
        await show(call, "\n".join(lines), _w2_card_acc_kb(t, accounts))
        return
    if action == "w2cardadd":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(call, t, state, form="cardadd", prompt_key="w2_card_add_ask", back_action="w2cardacc")
        return
    if action == "w2carddel":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        cfg = await shop_settings.load(session, shop.id)
        accounts = w2_card_accounts(cfg)
        if not 0 <= callback_data.id < len(accounts):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await shop_settings.set_many(
            session, shop.id, {"card_accounts": w2_card_without(accounts, callback_data.id)}
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.card_account_del",
            entity="shop",
            entity_id=shop.id,
            after={"index": callback_data.id},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        accounts = w2_card_accounts(cfg)
        await show(call, t("w2_card_acc_title", count=len(accounts)), _w2_card_acc_kb(t, accounts))
        return
    if action == "w2tech":
        await _w2_tech_show(call, session, shop, t, staff)
        return
    if action == "w2techt":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        if not 0 <= callback_data.id < len(W2_TECH_TOGGLES):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await _w2_set_bool(session, shop, staff, W2_TECH_TOGGLES[callback_data.id][0], "settings.tech_toggle")
        await call.answer(t("adm_saved"))
        await _w2_tech_show(call, session, shop, t, staff)
        return
    await w2_shop_dispatch(call, callback_data, session, shop, t, staff, cfg, state)


# --- wave 2 screens: shop / users / support / help / channels ----------------------


def _w2_list_kb(
    t: Texts,
    rows: list[tuple[int, str]],
    add_action: str,
    edit_action: str,
    del_action: str,
    back_action: str,
    can_write: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row_id, title in rows:
        kb.button(
            text=title[:64],
            callback_data=Admin(section="manage", action=edit_action, id=row_id),
        )
        if can_write:
            kb.button(
                text=t("w2_del", name=title)[:64],
                callback_data=Admin(section="manage", action=del_action, id=row_id),
            )
    if can_write:
        kb.button(text=t("w2_add"), callback_data=Admin(section="manage", action=add_action))
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action=back_action))
    kb.adjust(1)
    return kb.as_markup()


async def w2_shop_dispatch(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action == "w2shop":
        if not (staff.can("dashboard.view") or staff.can("catalog.read")):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        await _w2_shop_show(call, session, shop, t)
        return
    if action == "w2cats":
        if not staff.can("catalog.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        rows = list(
            await session.scalars(
                select(Category).where(Category.shop_id == shop.id).order_by(Category.sort, Category.id)
            )
        )
        await call.answer()
        await show(
            call,
            t("w2_cats_title", count=len(rows)),
            _w2_list_kb(
                t,
                [(r.id, r.title) for r in rows],
                "w2catadd",
                "w2catedit",
                "w2catdel",
                "w2shop",
                staff.can("catalog.write"),
            ),
        )
        return
    if action == "w2catadd":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        await _w2_ask(call, t, state, form="catadd", prompt_key="w2_cat_add_ask", back_action="w2cats")
        return
    if action == "w2catdel":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        row = await session.get(Category, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await session.delete(row)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.category_del",
            entity="category",
            entity_id=row.id,
            after={"title": row.title},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await w2_shop_dispatch(
            call, Admin(section="manage", action="w2cats"), session, shop, t, staff, cfg, state
        )
        return
    if action == "w2catedit":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        row = await session.get(Category, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await _w2_ask(
            call,
            t,
            state,
            form="catren",
            prompt_key="w2_cat_ren_ask",
            back_action="w2cats",
            extra={"id": row.id},
        )
        return
    if action == "w2plans":
        if not staff.can("catalog.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        rows = list(
            await session.scalars(
                select(Plan).where(Plan.shop_id == shop.id).order_by(Plan.sort, Plan.id).limit(40)
            )
        )
        await call.answer()
        lines = [t("w2_plans_title", count=len(rows))]
        for row in rows:
            mark = "" if row.is_active else "⛔ "
            lines.append(t("w2_plans_row", mark=mark, title=esc(row.title), price=money(row.price)))
        kb = InlineKeyboardBuilder()
        for row in rows:
            kb.button(
                text=row.title[:64], callback_data=Admin(section="manage", action="w2planedit", id=row.id)
            )
            if staff.can("catalog.write"):
                kb.button(
                    text=t("w2_del", name=row.title)[:64],
                    callback_data=Admin(section="manage", action="w2plandel", id=row.id),
                )
        if staff.can("catalog.write"):
            kb.button(text=t("w2_add"), callback_data=Admin(section="manage", action="w2planadd"))
        kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2shop"))
        kb.adjust(1)
        await show(call, "\n".join(lines), kb.as_markup())
        return
    if action == "w2planadd":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        await _w2_ask(call, t, state, form="planadd1", prompt_key="w2_plan_title_ask", back_action="w2plans")
        return
    if action == "w2plandel":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        row = await session.get(Plan, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        linked = int(
            await session.scalar(select(func.count(Service.id)).where(Service.plan_id == row.id)) or 0
        )
        if linked:
            await call.answer(t("w2_plan_has_services"), show_alert=True)
            return
        await session.delete(row)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.plan_del",
            entity="plan",
            entity_id=row.id,
            after={"title": row.title},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await w2_shop_dispatch(
            call, Admin(section="manage", action="w2plans"), session, shop, t, staff, cfg, state
        )
        return
    if action == "w2planedit":
        if not staff.can("catalog.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        row = await session.get(Plan, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await call.answer()
        text = t("w2_plan_title", title=esc(row.title), price=money(row.price), days=row.days, gb=row.data_gb)
        kb = InlineKeyboardBuilder()
        if staff.can("catalog.write"):
            for pos, field in enumerate(("price", "days", "gb", "title", "toggle")):
                kb.button(
                    text=t(f"w2_plan_{field}")[:64],
                    callback_data=Admin(section="manage", action="w2planfield", id=row.id, page=pos),
                )
        kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2plans"))
        kb.adjust(2, 2, 1, 1)
        await show(call, text, kb.as_markup())
        return
    if action == "w2planfield":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        fields = ("price", "days", "gb", "title", "toggle")
        if not 0 <= callback_data.page < len(fields):
            await call.answer(t("button_expired"), show_alert=True)
            return
        field = fields[callback_data.page]
        row = await session.get(Plan, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        if field == "toggle":
            row.is_active = not row.is_active
            await record(
                session,
                shop_id=shop.id,
                actor=actor(staff),
                action="catalog.plan_toggle",
                entity="plan",
                entity_id=row.id,
                after={"is_active": row.is_active},
            )
            await session.commit()
            await call.answer(t("adm_saved"))
            await w2_shop_dispatch(
                call,
                Admin(section="manage", action="w2planedit", id=row.id),
                session,
                shop,
                t,
                staff,
                cfg,
                state,
            )
            return
        await _w2_ask(
            call,
            t,
            state,
            form="planfield",
            prompt_key=f"w2_plan_{field}_ask",
            back_action="w2planedit",
            back_id=row.id,
            extra={"id": row.id, "field": field},
        )
        return
    if action == "w2gifts":
        if not staff.can("catalog.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        rows = list(
            await session.scalars(
                select(GiftCode).where(GiftCode.shop_id == shop.id).order_by(GiftCode.id.desc()).limit(20)
            )
        )
        await call.answer()
        lines = [t("w2_gifts_title", count=len(rows))]
        for row in rows:
            mark = "" if row.is_active else "⛔ "
            lines.append(
                t(
                    "w2_gifts_row",
                    mark=mark,
                    code=esc(row.code),
                    amount=money(row.amount),
                    used=row.used_count,
                    total=row.max_uses or "∞",
                )
            )
        kb = InlineKeyboardBuilder()
        if staff.can("catalog.write"):
            kb.button(text=t("w2_add"), callback_data=Admin(section="manage", action="w2giftadd"))
            for row in rows:
                kb.button(
                    text=t("w2_del", name=row.code)[:64],
                    callback_data=Admin(section="manage", action="w2giftdel", id=row.id),
                )
        kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2shop"))
        kb.adjust(1)
        await show(call, "\n".join(lines), kb.as_markup())
        return
    if action == "w2giftadd":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        await _w2_ask(call, t, state, form="giftadd1", prompt_key="w2_gift_code_ask", back_action="w2gifts")
        return
    if action == "w2giftdel":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        row = await session.get(GiftCode, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        row.is_active = False
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.gift_del",
            entity="gift_code",
            entity_id=row.id,
            after={"code": row.code},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await w2_shop_dispatch(
            call, Admin(section="manage", action="w2gifts"), session, shop, t, staff, cfg, state
        )
        return
    if action == "w2discs":
        if not staff.can("catalog.read"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        rows = list(
            await session.scalars(
                select(DiscountCode)
                .where(DiscountCode.shop_id == shop.id)
                .order_by(DiscountCode.id.desc())
                .limit(20)
            )
        )
        await call.answer()
        lines = [t("w2_discs_title", count=len(rows))]
        for row in rows:
            mark = "" if row.is_active else "⛔ "
            lines.append(
                t(
                    "w2_discs_row",
                    mark=mark,
                    code=esc(row.code),
                    value=row.value,
                    used=row.used_count,
                    total=row.max_uses or "∞",
                )
            )
        kb = InlineKeyboardBuilder()
        if staff.can("catalog.write"):
            kb.button(text=t("w2_add"), callback_data=Admin(section="manage", action="w2discadd"))
            for row in rows:
                kb.button(
                    text=t("w2_del", name=row.code)[:64],
                    callback_data=Admin(section="manage", action="w2discdel", id=row.id),
                )
        kb.button(text=t("btn_back"), callback_data=Admin(section="manage", action="w2shop"))
        kb.adjust(1)
        await show(call, "\n".join(lines), kb.as_markup())
        return
    if action == "w2discadd":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        await _w2_ask(call, t, state, form="discadd1", prompt_key="w2_disc_code_ask", back_action="w2discs")
        return
    if action == "w2discdel":
        if not await _w2_need(staff, call, t, "catalog.write"):
            return
        row = await session.get(DiscountCode, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        row.is_active = False
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.discount_del",
            entity="discount_code",
            entity_id=row.id,
            after={"code": row.code},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await w2_shop_dispatch(
            call, Admin(section="manage", action="w2discs"), session, shop, t, staff, cfg, state
        )
        return
    if action == "w2bulk":
        await call.answer()
        await show(
            call,
            t(
                "w2_bulk_title",
                enabled=w2_on(cfg.get("bulk_buy_enabled")),
                limit=int(cfg.get("bulk_buy_max") or 0),
            ),
            keyboards.back(t, Admin(section="manage", action="w2shop").pack()),
        )
        return
    await w2_people_dispatch(call, callback_data, session, shop, t, staff, cfg, state)


# --- wave 2 screens: people / support / help / channels + value forms ---------------


async def w2_people_dispatch(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action == "w2users":
        await _w2_users_show(call, session, shop, t, staff)
        return
    if action == "w2staffadd":
        if not await _w2_need(staff, call, t, "staff.manage"):
            return
        await _w2_ask(call, t, state, form="staffadd1", prompt_key="w2_staff_id_ask", back_action="w2users")
        return
    if action == "w2staffdel":
        if not await _w2_need(staff, call, t, "staff.manage"):
            return
        row = await session.get(Staff, callback_data.id)
        if row is None or row.shop_id != shop.id or (staff.id is not None and row.id == staff.id):
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        row.is_active = False
        row.session_version += 1
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="staff.deactivate",
            entity="staff",
            entity_id=row.id,
            after={},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _w2_users_show(call, session, shop, t, staff)
        return
    if action == "w2staffrole":
        if not await _w2_need(staff, call, t, "staff.manage"):
            return
        roles = (StaffRole.ADMIN, StaffRole.SUPPORT, StaffRole.FINANCE)
        if not 0 <= callback_data.page < len(roles):
            await call.answer(t("button_expired"), show_alert=True)
            return
        telegram_id = callback_data.id
        existing = await staff_service.by_telegram(session, shop.id, telegram_id)
        try:
            if existing is not None:
                existing.is_active = True
                existing.role = roles[callback_data.page]
                row = existing
            else:
                row = await staff_service.create_staff(
                    session,
                    shop.id,
                    role=roles[callback_data.page],
                    username=None,
                    password=None,
                    telegram_id=telegram_id,
                )
            await session.flush()
        except staff_service.AuthError:
            await session.rollback()
            await call.answer(t("w2_staff_exists"), show_alert=True)
            return
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="staff.add",
            entity="staff",
            entity_id=row.id,
            after={"role": row.role.value},
        )
        await session.commit()
        await state.clear()
        await call.answer(t("adm_saved"))
        await _w2_users_show(call, session, shop, t, staff)
        return
    if action == "w2support":
        await _w2_support_show(call, session, shop, t, staff)
        return
    if action == "w2supuser":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(
            call,
            t,
            state,
            form="strspec",
            prompt_key="w2_sup_user_ask",
            back_action="w2support",
            extra={"spec": "support_username", "maxlen": 64, "audit": "settings.support"},
        )
        return
    if action == "w2deptadd":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(call, t, state, form="deptadd", prompt_key="w2_dept_add_ask", back_action="w2support")
        return
    if action == "w2deptdel":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        depts = w2_support_departments(cfg)
        if not 0 <= callback_data.id < len(depts):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await shop_settings.set_many(
            session, shop.id, {"support_departments": w2_without_department(cfg, depts[callback_data.id])}
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.dept_del",
            entity="shop",
            entity_id=shop.id,
            after={"name": depts[callback_data.id]},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _w2_support_show(call, session, shop, t, staff)
        return
    if action == "w2contactadd":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(
            call, t, state, form="contactadd1", prompt_key="w2_contact_title_ask", back_action="w2support"
        )
        return
    if action == "w2contactdel":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        row = await session.get(SupportContact, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await session.delete(row)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.contact_del",
            entity="support_contact",
            entity_id=row.id,
            after={"title": row.title},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _w2_support_show(call, session, shop, t, staff)
        return
    if action == "w2help":
        await _w2_help_show(call, session, shop, t, staff)
        return
    if action == "w2helpedit":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        row = await session.get(BotText, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await _w2_ask(
            call,
            t,
            state,
            form="helpedit",
            prompt_key="w2_help_value_ask",
            back_action="w2help",
            extra={"id": row.id},
        )
        return
    if action == "w2helpdel":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        row = await session.get(BotText, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await session.delete(row)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.text_reset",
            entity="bot_text",
            entity_id=row.id,
            after={"key": row.key, "locale": row.locale},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _w2_help_show(call, session, shop, t, staff)
        return
    if action == "w2helpnew":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        if not 0 <= callback_data.id < len(W2_HELP_KEYS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await _w2_ask(
            call,
            t,
            state,
            form="helpnew",
            prompt_key="w2_help_value_ask",
            back_action="w2help",
            extra={"key": W2_HELP_KEYS[callback_data.id]},
        )
        return
    if action == "w2channels":
        await _w2_channels_show(call, session, shop, t, staff)
        return
    if action == "w2chadd":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(call, t, state, form="chadd1", prompt_key="w2_ch_title_ask", back_action="w2channels")
        return
    if action == "w2chdel":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        row = await session.get(ForcedChannel, callback_data.id)
        if row is None or row.shop_id != shop.id:
            await call.answer(t("mgmt_not_found"), show_alert=True)
            return
        await session.delete(row)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.channel_del",
            entity="forced_channel",
            entity_id=row.id,
            after={"title": row.title},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        await _w2_channels_show(call, session, shop, t, staff)
        return
    if action == "w2logset":
        if not await _w2_need(staff, call, t, "settings.write"):
            return
        await _w2_ask(call, t, state, form="logset", prompt_key="w2_log_set_ask", back_action="w2channels")
        return
    await call.answer()


async def _w2_apply_intspec(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
) -> str | None:
    value = parse_int(to_latin_digits(message.text or ""))
    lo, hi = int(data.get("min") or 0), int(data.get("max") or 0)
    if value is None or not lo <= value <= hi:
        await message.answer(t("mgmt_input_invalid"))
        return None
    spec = str(data["spec"])
    await shop_settings.set_many(session, shop.id, {spec: value})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=str(data.get("audit") or "settings.set"),
        entity="shop",
        entity_id=shop.id,
        after={spec: value},
    )
    await session.commit()
    return spec


async def _w2_apply_strspec(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
) -> str | None:
    value = (message.text or "").strip()[: int(data.get("maxlen") or 255)]
    if not value:
        await message.answer(t("mgmt_input_invalid"))
        return None
    spec = str(data["spec"])
    await shop_settings.set_many(session, shop.id, {spec: value})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=str(data.get("audit") or "settings.set"),
        entity="shop",
        entity_id=shop.id,
        after={spec: "***" if "secret" in spec or "token" in spec else value},
    )
    await session.commit()
    return spec


@router.message(ManageStates.w2_value, F.text)
async def w2_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    bot: Bot,
) -> None:
    data = await state.get_data()
    form = str(data.get("w2form") or "")

    async def back() -> None:
        await state.clear()
        await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))

    if form in ("intspec", "strspec"):
        if not staff.can("settings.write"):
            await message.answer(t("adm_forbidden"))
            return
        ok = await (_w2_apply_intspec if form == "intspec" else _w2_apply_strspec)(
            message, data, session, shop, t, staff, cfg
        )
        if ok is None:
            return
        await back()
        return
    if form == "cardadd":
        if not staff.can("settings.write"):
            await message.answer(t("adm_forbidden"))
            return
        parts = (message.text or "").split()
        number = "".join(ch for ch in (parts[0] if parts else "") if ch.isdigit())
        owner = " ".join(parts[1:])[:48]
        if len(number) < 12 or not luhn_ok(number):
            await message.answer(t("w2_card_bad_number"))
            return
        accounts = w2_card_accounts(cfg) + [{"number": number, "owner": owner}]
        await shop_settings.set_many(session, shop.id, {"card_accounts": accounts})
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.card_account_add",
            entity="shop",
            entity_id=shop.id,
            after={"card": masked(number)},
        )
        await session.commit()
        await back()
        return
    if form == "catadd":
        title = (message.text or "").strip()[:64]
        if not title:
            await message.answer(t("mgmt_input_invalid"))
            return
        session.add(Category(shop_id=shop.id, title=title))
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.category_add",
            entity="category",
            entity_id=0,
            after={"title": title},
        )
        await session.commit()
        await back()
        return
    if form == "catren":
        title = (message.text or "").strip()[:64]
        row = await session.get(Category, int(data.get("id") or 0))
        if not title or row is None or row.shop_id != shop.id:
            await message.answer(t("mgmt_input_invalid"))
            return
        row.title = title
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="catalog.category_rename",
            entity="category",
            entity_id=row.id,
            after={"title": title},
        )
        await session.commit()
        await back()
        return
    if form in ("planadd1", "planadd2", "planadd3", "planadd4"):
        await _w2_plan_wizard(message, data, session, shop, t, staff, cfg, state, form)
        return
    if form == "planfield":
        await _w2_plan_field(message, data, session, shop, t, staff, cfg, state)
        return
    if form in ("giftadd1", "giftadd2", "giftadd3"):
        await _w2_gift_wizard(message, data, session, shop, t, staff, cfg, state, form)
        return
    if form in ("discadd1", "discadd2", "discadd3"):
        await _w2_disc_wizard(message, data, session, shop, t, staff, cfg, state, form)
        return
    if form in ("staffadd1", "staffadd2"):
        await _w2_staff_wizard(message, data, session, shop, t, staff, cfg, state, form)
        return
    if form == "deptadd":
        name = (message.text or "").strip()[:48]
        if not name:
            await message.answer(t("mgmt_input_invalid"))
            return
        await shop_settings.set_many(session, shop.id, {"support_departments": w2_with_department(cfg, name)})
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.dept_add",
            entity="shop",
            entity_id=shop.id,
            after={"name": name},
        )
        await session.commit()
        await back()
        return
    if form in ("contactadd1", "contactadd2"):
        title = (message.text or "").strip()[:64] if form == "contactadd1" else str(data.get("title") or "")
        if form == "contactadd1":
            if not title:
                await message.answer(t("mgmt_input_invalid"))
                return
            await state.update_data(title=title, w2form="contactadd2")
            await message.answer(t("w2_contact_url_ask"), reply_markup=keyboards.cancel(t))
            return
        url = (message.text or "").strip()[:255]
        if not url:
            await message.answer(t("mgmt_input_invalid"))
            return
        session.add(SupportContact(shop_id=shop.id, title=title, url=url))
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.contact_add",
            entity="support_contact",
            entity_id=0,
            after={"title": title},
        )
        await session.commit()
        await back()
        return
    if form in ("helpedit", "helpnew"):
        value = (message.text or "").strip()
        if not value:
            await message.answer(t("mgmt_input_invalid"))
            return
        if form == "helpedit":
            row = await session.get(BotText, int(data.get("id") or 0))
            if row is None or row.shop_id != shop.id:
                await message.answer(t("mgmt_not_found"))
                return
            row.value = value
            key, locale = row.key, row.locale
            entity_id = row.id
        else:
            key = str(data.get("key") or "")
            locale = "fa"
            row = await session.scalar(
                select(BotText).where(
                    BotText.shop_id == shop.id, BotText.locale == locale, BotText.key == key
                )
            )
            if row is None:
                row = BotText(shop_id=shop.id, locale=locale, key=key, value=value)
                session.add(row)
            else:
                row.value = value
            await session.flush()
            entity_id = row.id
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.text_set",
            entity="bot_text",
            entity_id=entity_id,
            after={"key": key, "locale": locale},
        )
        await session.commit()
        await back()
        return
    if form in ("chadd1", "chadd2", "chadd3"):
        await _w2_channel_wizard(message, data, session, shop, t, staff, cfg, state, form)
        return
    if form == "logset":
        try:
            chat_id = int((message.text or "").strip())
        except ValueError:
            await message.answer(t("mgmt_input_invalid"))
            return
        row = await session.scalar(
            select(LogChannel).where(LogChannel.shop_id == shop.id, LogChannel.topic == "reports")
        )
        if row is None:
            row = LogChannel(shop_id=shop.id, topic="reports", chat_id=chat_id)
            session.add(row)
        else:
            row.chat_id = chat_id
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="settings.log_set",
            entity="log_channel",
            entity_id=row.id or 0,
            after={"topic": "reports"},
        )
        await session.commit()
        await back()
        return
    await message.answer(t("mgmt_input_invalid"))


async def _w2_plan_wizard(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    form: str,
) -> None:
    if form == "planadd1":
        title = (message.text or "").strip()[:96]
        if not title:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(title=title, w2form="planadd2")
        await message.answer(t("w2_plan_price_ask"), reply_markup=keyboards.cancel(t))
        return
    if form == "planadd2":
        price = parse_int(to_latin_digits(message.text or ""))
        if price is None or not 0 <= price <= 1_000_000_000:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(price=price, w2form="planadd3")
        await message.answer(t("w2_plan_days_ask"), reply_markup=keyboards.cancel(t))
        return
    if form == "planadd3":
        days = parse_int(to_latin_digits(message.text or ""))
        if days is None or not 1 <= days <= 3650:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(days=days, w2form="planadd4")
        await message.answer(t("w2_plan_gb_ask"), reply_markup=keyboards.cancel(t))
        return
    gb = parse_int(to_latin_digits(message.text or ""))
    if gb is None or not 0 <= gb <= 100_000:
        await message.answer(t("mgmt_input_invalid"))
        return
    row = Plan(
        shop_id=shop.id,
        title=str(data.get("title") or "-")[:96],
        price=int(data.get("price") or 0),
        days=int(data.get("days") or 30),
        data_gb=gb,
    )
    session.add(row)
    await session.flush()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="catalog.plan_add",
        entity="plan",
        entity_id=row.id,
        after={"title": row.title},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


async def _w2_plan_field(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    row = await session.get(Plan, int(data.get("id") or 0))
    field = str(data.get("field") or "")
    if row is None or row.shop_id != shop.id or field not in ("price", "days", "gb", "title"):
        await message.answer(t("mgmt_not_found"))
        return
    if field == "title":
        title = (message.text or "").strip()[:96]
        if not title:
            await message.answer(t("mgmt_input_invalid"))
            return
        row.title = title
    else:
        value = parse_int(to_latin_digits(message.text or ""))
        bounds = {"price": (0, 1_000_000_000), "days": (1, 3650), "gb": (0, 100_000)}[field]
        if value is None or not bounds[0] <= value <= bounds[1]:
            await message.answer(t("mgmt_input_invalid"))
            return
        setattr(row, {"price": "price", "days": "days", "gb": "data_gb"}[field], value)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="catalog.plan_edit",
        entity="plan",
        entity_id=row.id,
        after={field: str(getattr(row, field if field != "gb" else "data_gb", ""))[:64]},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


async def _w2_gift_wizard(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    form: str,
) -> None:
    if form == "giftadd1":
        code = (message.text or "").strip().upper()[:32]
        if len(code) < 3:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(code=code, w2form="giftadd2")
        await message.answer(t("w2_gift_amount_ask"), reply_markup=keyboards.cancel(t))
        return
    if form == "giftadd2":
        amount = parse_int(to_latin_digits(message.text or ""))
        if amount is None or not 1 <= amount <= 1_000_000_000:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(amount=amount, w2form="giftadd3")
        await message.answer(t("w2_gift_uses_ask"), reply_markup=keyboards.cancel(t))
        return
    uses = parse_int(to_latin_digits(message.text or ""))
    if uses is None or not 0 <= uses <= 1_000_000:
        await message.answer(t("mgmt_input_invalid"))
        return
    try:
        row = GiftCode(
            shop_id=shop.id,
            code=str(data.get("code") or ""),
            amount=int(data.get("amount") or 0),
            max_uses=uses or None,
        )
        session.add(row)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await message.answer(t("w2_code_taken"))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="catalog.gift_add",
        entity="gift_code",
        entity_id=row.id,
        after={"code": row.code},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


async def _w2_disc_wizard(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    form: str,
) -> None:
    if form == "discadd1":
        code = (message.text or "").strip().upper()[:32]
        if len(code) < 3:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(code=code, w2form="discadd2")
        await message.answer(t("w2_disc_percent_ask"), reply_markup=keyboards.cancel(t))
        return
    if form == "discadd2":
        percent = parse_int(to_latin_digits(message.text or ""))
        if percent is None or not 1 <= percent <= 100:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(percent=percent, w2form="discadd3")
        await message.answer(t("w2_disc_uses_ask"), reply_markup=keyboards.cancel(t))
        return
    uses = parse_int(to_latin_digits(message.text or ""))
    if uses is None or not 0 <= uses <= 1_000_000:
        await message.answer(t("mgmt_input_invalid"))
        return
    try:
        row = DiscountCode(
            shop_id=shop.id,
            code=str(data.get("code") or ""),
            kind=DiscountKind.PERCENT,
            value=int(data.get("percent") or 0),
            max_uses=uses or None,
        )
        session.add(row)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await message.answer(t("w2_code_taken"))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="catalog.discount_add",
        entity="discount_code",
        entity_id=row.id,
        after={"code": row.code},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


async def _w2_staff_wizard(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    form: str,
) -> None:
    if form == "staffadd1":
        try:
            telegram_id = int((message.text or "").strip())
        except ValueError:
            await message.answer(t("mgmt_input_invalid"))
            return
        if telegram_id <= 0:
            await message.answer(t("mgmt_input_invalid"))
            return
        existing = await staff_service.by_telegram(session, shop.id, telegram_id)
        if existing is not None and existing.is_active:
            await message.answer(t("w2_staff_exists"))
            return
        await state.update_data(telegram_id=telegram_id, w2form="staffadd2")
        kb = InlineKeyboardBuilder()
        for pos, role in enumerate((StaffRole.ADMIN, StaffRole.SUPPORT, StaffRole.FINANCE)):
            kb.button(
                text=role.value,
                callback_data=Admin(section="manage", action="w2staffrole", id=telegram_id, page=pos),
            )
        kb.button(text=t("btn_cancel"), callback_data=Admin(section="manage", action="w2users"))
        kb.adjust(3, 1)
        await message.answer(t("w2_staff_role_ask"), reply_markup=kb.as_markup())
        return
    await message.answer(t("mgmt_input_invalid"))


async def _w2_channel_wizard(
    message: Message,
    data: dict[str, Any],
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
    form: str,
) -> None:
    if form == "chadd1":
        title = (message.text or "").strip()[:96]
        if not title:
            await message.answer(t("mgmt_input_invalid"))
            return
        await state.update_data(title=title, w2form="chadd2")
        await message.answer(t("w2_ch_link_ask"), reply_markup=keyboards.cancel(t))
        return
    if form == "chadd2":
        url = (message.text or "").strip()[:255]
        if not url.startswith("https://t.me/"):
            await message.answer(t("w2_ch_link_bad"))
            return
        await state.update_data(url=url, w2form="chadd3")
        await message.answer(t("w2_ch_id_ask"), reply_markup=keyboards.cancel(t))
        return
    chat_id = (message.text or "").strip()[:64]
    if not chat_id:
        await message.answer(t("mgmt_input_invalid"))
        return
    try:
        row = ForcedChannel(
            shop_id=shop.id,
            chat_id=chat_id,
            title=str(data.get("title") or "")[:96],
            url=str(data.get("url") or "")[:255],
        )
        session.add(row)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await message.answer(t("w2_code_taken"))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.channel_add",
        entity="forced_channel",
        entity_id=row.id,
        after={"title": row.title},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
