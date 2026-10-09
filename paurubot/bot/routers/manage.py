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
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_tools import _slice_figures, stats_window
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ManageStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core.formatting import esc, jdate, money, parse_int, size, to_latin_digits
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerAccountKind,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    ResellerProfile,
    Service,
    ServiceStatus,
    TrialGrant,
    UsernameMethod,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.panels import PanelError
from paurubot.panels import drop as drop_driver
from paurubot.panels.registry import visible_in_shop
from paurubot.provisioning import monitor

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


def pulse_window(
    key: str, now: datetime, custom: datetime | None = None
) -> tuple[datetime, datetime | None]:
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
    sales, sales_amount, topups, topup_amount, joined = await _slice_figures(
        session, shop_id, since, until
    )
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
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop_id, Customer.telegram_id > 0
            ),
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
        await _hub(call, session, shop, t)
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
    await call.answer()


async def _hub(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    now = utcnow()
    since, _ = stats_window("today", now)
    sales, sales_amount, topups, topup_amount, _ = await _slice_figures(session, shop.id, since, None)
    healthy = await _count(
        session,
        select(func.count(Panel.id)).where(
            Panel.shop_id == shop.id, Panel.is_active.is_(True), Panel.is_healthy.is_(True)
        ),
    )
    total = await _count(session, select(func.count(Panel.id)).where(Panel.shop_id == shop.id))
    text = "\n\n".join(
        [
            t("mgmt_hub_title", date=jdate(now)),
            t(
                "mgmt_pulse_trade",
                sales=sales,
                sales_amount=money(sales_amount),
                topups=topups,
                topup_amount=money(topup_amount),
            ),
            t("mgmt_panels_title", count=total, healthy=healthy),
        ]
    )
    await call.answer()
    await show(call, text, hub_kb(t))


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


async def _panels(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    panels = list(
        await session.scalars(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id))
    )
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
            callback_data=Admin(section="manage", action="panel", id=panel.id),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="manage"))
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
    await show(
        call,
        _panel_text(
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
        ),
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
        await _panel(call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg)
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
        await _panel(call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg)
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
        await _delete(call, callback_data, session, shop, t, staff, panel)
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
        await show(call, _extras_text(t, panel, cfg), _extras_kb(t, panel))
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
        await _panel(call, Admin(section="manage", action="panel", id=panel.id), session, shop, t, staff, cfg)
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
        await show(call, t("mgmt_flags_title"), _flags_kb(t, panel))
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
) -> None:
    if not callback_data.page:
        await call.answer()
        await show(
            call,
            t("mgmt_delete_ask", name=esc(panel.name)),
            keyboards.confirm(
                t,
                Admin(section="manage", action="delete", id=panel.id, page=1).pack(),
                Admin(section="manage", action="panel", id=panel.id).pack(),
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
            keyboards.back(t, Admin(section="manage", action="panel", id=panel.id).pack()),
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
    await _panels(call, session, shop, t)


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
