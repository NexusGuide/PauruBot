"""Per-panel management: explicit setters, audited settings, real driver capabilities."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Admin, Nav
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.utils import StaffOnly, show
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    Service,
    ServiceStatus,
    UsernameMethod,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.panels import PanelError, registry
from paurubot.provisioning.lifecycle import renew_mode

router = Router(name="admin_panels")
router.callback_query.filter(StaffOnly())
router.message.filter(StaffOnly())
PAGE = 10


class PanelAdminStates(StatesGroup):
    value = State()


@dataclass(frozen=True)
class Feature:
    key: str
    change: str | None = None


FEATURES = (
    Feature("is_active"),
    Feature("allow_trials"),
    Feature("allow_renewals", "renew"),
    Feature("sell_extra_volume", "extra_volume"),
    Feature("sell_extra_days", "extra_days"),
    Feature("renew_resets_usage", "renew"),
)
CHANGES = ("renew", "upgrade", "extra_volume", "extra_days", "gift", "revoke")
METHODS = tuple(UsernameMethod)


def btn(
    t: Texts, key: str, section: str, panel_id: int = 0, action: str = "show", page: int = 0
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=t(key), callback_data=Admin(section=section, action=action, id=panel_id, page=page).pack()
    )


async def panel_owned(
    session: AsyncSession, shop_id: int, panel_id: int, *, lock: bool = False
) -> Panel | None:
    stmt = select(Panel).where(Panel.id == panel_id, Panel.shop_id == shop_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(stmt)


def enabled(panel: Panel, feature: Feature, cfg: dict[str, Any]) -> bool:
    if feature.key == "is_active":
        return panel.is_active
    if feature.key == "renew_resets_usage":
        return renew_mode(None, panel, cfg).endswith("_reset")
    return (panel.options or {}).get(feature.key) is not False


def management_kb(t: Texts, panel: Panel, staff: StaffIdentity) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(t, "adm_pm_features", "pfeat", panel.id))
    kb.row(btn(t, "adm_pm_capabilities", "pmgr", panel.id, "caps"))
    if staff.can("panels.write"):
        kb.row(
            btn(t, "adm_pm_name", "pmgr", panel.id, "name"), btn(t, "adm_pm_test", "pmgr", panel.id, "test")
        )
        kb.row(
            btn(t, "adm_pcfg_method", "pmgr", panel.id, "methods"),
            btn(t, "adm_pcfg_capacity", "pmgr", panel.id, "capacity"),
        )
        controls = [btn(t, "adm_pcfg_prefix", "pmgr", panel.id, "prefix")]
        if "renew" in registry.changes_of(panel.kind):
            controls.append(btn(t, "adm_pm_reset", "pmgr", panel.id, "reset"))
        kb.row(*controls)
    if staff.can("catalog.write"):
        kb.row(btn(t, "adm_pm_plans", "pmgr", panel.id, "plans"))
    if staff.can("reports.read"):
        kb.row(btn(t, "adm_pm_finance", "pmgr", panel.id, "finance"))
    public = get_settings().public_url.rstrip("/")
    if staff.can("panels.write") and public:
        kb.row(InlineKeyboardButton(text=t("adm_pm_secure_web"), url=f"{public}/admin/panels"))
    kb.row(btn(t, "adm_pm_back_panels", "panels"), btn(t, "adm_ud_admin", "home"))
    kb.row(btn(t, "adm_ud_close", "pmgr", panel.id, "close"))
    return kb.as_markup()


async def panel_text(session: AsyncSession, t: Texts, panel: Panel) -> str:
    counts = await session.execute(
        select(Service.status, func.count(Service.id))
        .where(
            Service.shop_id == panel.shop_id,
            Service.panel_id == panel.id,
            Service.status != ServiceStatus.DELETED,
        )
        .group_by(Service.status)
    )
    statuses = dict(counts.all())
    plans = int(
        await session.scalar(
            select(func.count(Plan.id)).where(Plan.shop_id == panel.shop_id, Plan.panel_id == panel.id)
        )
        or 0
    )
    return t(
        "adm_pm_card",
        name=esc(panel.name),
        id=panel.id,
        kind=esc(panel.kind.value),
        state=t("adm_on") if panel.is_active else t("adm_off"),
        health=t("adm_pm_unknown")
        if panel.is_healthy is None
        else t("adm_pm_ok" if panel.is_healthy else "adm_pm_down"),
        checked=panel.health_checked_at.strftime("%Y-%m-%d %H:%M UTC") if panel.health_checked_at else "—",
        services=sum(statuses.values()),
        active=statuses.get(ServiceStatus.ACTIVE, 0),
        plans=plans,
        capacity=panel.capacity or t("unlimited"),
        method=t(f"adm_pcfg_method_{panel.username_method.value}"),
        prefix=esc(panel.username_prefix or "—"),
    )


def features_kb(t: Texts, panel: Panel, staff: StaffIdentity, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    changes = registry.changes_of(panel.kind)
    for index, feature in enumerate(FEATURES):
        supported = feature.change is None or feature.change in changes
        value = enabled(panel, feature, cfg)
        text = (
            t("adm_pm_unsupported") if not supported else t("adm_pm_enabled" if value else "adm_pm_disabled")
        )
        if (
            feature.key == "renew_resets_usage"
            and (panel.options or {}).get(feature.key) is None
            and supported
        ):
            text = t("adm_pm_inherit", state=text)
        target = (
            Admin(section="pfeat", action="on" if not value else "off", id=panel.id, page=index).pack()
            if supported and staff.can("panels.write")
            else Nav(to="noop").pack()
        )
        kb.row(
            InlineKeyboardButton(text=text, callback_data=target),
            InlineKeyboardButton(text=t("adm_pm_f_" + feature.key), callback_data=target),
        )
    if staff.can("panels.write") and "renew" in changes:
        kb.row(btn(t, "adm_pm_reset_inherit", "pfeat", panel.id, "inherit", 5))
    kb.row(btn(t, "adm_pm_capabilities", "pmgr", panel.id, "caps"))
    kb.row(btn(t, "btn_back", "pmgr", panel.id), btn(t, "adm_ud_admin", "home"))
    kb.row(btn(t, "adm_ud_close", "pmgr", panel.id, "close"))
    return kb.as_markup()


async def audit(
    session: AsyncSession,
    shop: ShopRow,
    staff: StaffIdentity,
    panel: Panel,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="panel.bot_update",
        entity="panel",
        entity_id=panel.id,
        before=before,
        after=after,
    )
    await session.commit()


@router.callback_query(Admin.filter(F.section == "pfeat"), StaffOnly("panels.read"))
async def features(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    action = callback_data.action
    if action not in {"show", "on", "off", "inherit"}:
        await call.answer(t("button_expired"), show_alert=True)
        return
    write = action != "show"
    if write and not staff.can("panels.write"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    panel = await panel_owned(session, shop.id, callback_data.id, lock=write)
    if panel is None:
        await call.answer(t("button_expired"), show_alert=True)
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if write:
        if not 0 <= callback_data.page < len(FEATURES):
            await call.answer(t("button_expired"), show_alert=True)
            return
        feature = FEATURES[callback_data.page]
        if (feature.change and feature.change not in registry.changes_of(panel.kind)) or (
            action == "inherit" and feature.key != "renew_resets_usage"
        ):
            await call.answer(t("adm_pm_unsupported"), show_alert=True)
            return
        before = {
            feature.key: panel.is_active
            if feature.key == "is_active"
            else (panel.options or {}).get(feature.key)
        }
        value: bool | None = None if action == "inherit" else action == "on"
        if before[feature.key] != value:
            if feature.key == "is_active":
                panel.is_active = bool(value)
            else:
                options = dict(panel.options or {})
                if value is None:
                    options.pop(feature.key, None)
                else:
                    options[feature.key] = value
                panel.options = options
            await audit(session, shop, staff, panel, before, {feature.key: value})
        else:
            await session.commit()  # release the lock without duplicate audit entries
    await call.answer(t("adm_saved") if write else None)
    await show(call, t("adm_pm_features_head", name=esc(panel.name)), features_kb(t, panel, staff, cfg))


@router.callback_query(Admin.filter(F.section == "pmgr"), StaffOnly("panels.read"))
async def management(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    permission = {
        "name": "panels.write",
        "capacity": "panels.write",
        "prefix": "panels.write",
        "methods": "panels.write",
        "method": "panels.write",
        "test": "panels.write",
        "reset": "panels.write",
        "finance": "reports.read",
        "plans": "catalog.write",
    }.get(action)
    if permission and not staff.can(permission):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    panel = await panel_owned(session, shop.id, callback_data.id, lock=action == "method")
    if panel is None:
        await call.answer(t("button_expired"), show_alert=True)
        return
    if action == "close":
        await state.clear()
        await call.answer()
        await show(call, t("adm_pm_closed"))
        return
    if action in {"name", "capacity", "prefix"}:
        await state.clear()
        await state.set_state(PanelAdminStates.value)
        await state.update_data(panel_id=panel.id, panel_shop_id=shop.id, panel_field=action)
        await call.answer()
        await show(
            call,
            t("adm_pm_ask_" + action),
            InlineKeyboardMarkup(inline_keyboard=[[btn(t, "btn_cancel", "pmgr", panel.id)]]),
        )
        return
    if action in {"methods", "method"}:
        if action == "method":
            if not 0 <= callback_data.page < len(METHODS):
                await call.answer(t("button_expired"), show_alert=True)
                return
            before = {"username_method": panel.username_method.value}
            selected = METHODS[callback_data.page]
            if panel.username_method != selected:
                panel.username_method = selected
                await audit(session, shop, staff, panel, before, {"username_method": selected.value})
            else:
                await session.commit()
        kb = InlineKeyboardBuilder()
        for index, method in enumerate(METHODS):
            kb.row(
                InlineKeyboardButton(
                    text=("✅ " if method == panel.username_method else "")
                    + t(f"adm_pcfg_method_{method.value}"),
                    callback_data=Admin(section="pmgr", action="method", id=panel.id, page=index).pack(),
                )
            )
        kb.row(btn(t, "btn_back", "pmgr", panel.id))
        await call.answer(t("adm_saved") if action == "method" else None)
        await show(call, t("adm_pm_method_help"), kb.as_markup())
        return
    if action == "test":
        await call.answer(t("adm_pm_testing"))
        try:
            driver = await registry.driver_for(panel)
            health = await asyncio.wait_for(driver.health_check(), timeout=20)
            panel.is_healthy = bool(health.ok)
        except (PanelError, TimeoutError):
            panel.is_healthy = False
        panel.health_checked_at = utcnow()
        panel.health_error = None if panel.is_healthy else "bot_health_check_failed"
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="panel.bot_check",
            entity="panel",
            entity_id=panel.id,
            after={"ok": panel.is_healthy},
        )
        await session.commit()
        await show(
            call,
            t("adm_pm_test_ok" if panel.is_healthy else "adm_pm_test_fail")
            + "\n\n"
            + await panel_text(session, t, panel),
            management_kb(t, panel, staff),
        )
        return
    if action == "caps":
        kb = InlineKeyboardBuilder()
        for key in CHANGES:
            kb.row(
                InlineKeyboardButton(
                    text=t(
                        "adm_pm_supported" if key in registry.changes_of(panel.kind) else "adm_pm_unsupported"
                    ),
                    callback_data=Nav(to="noop").pack(),
                ),
                InlineKeyboardButton(text=t("adm_pm_c_" + key), callback_data=Nav(to="noop").pack()),
            )
        kb.row(btn(t, "btn_back", "pmgr", panel.id))
        await call.answer()
        await show(
            call,
            t("adm_pm_caps_head", name=esc(panel.name), delivery=esc(registry.delivery_of(panel.kind))),
            kb.as_markup(),
        )
        return
    if action == "reset":
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        kb = InlineKeyboardBuilder()
        kb.row(
            btn(t, "adm_pm_enabled", "pfeat", panel.id, "on", 5),
            btn(t, "adm_pm_disabled", "pfeat", panel.id, "off", 5),
        )
        kb.row(btn(t, "adm_pm_reset_inherit", "pfeat", panel.id, "inherit", 5))
        kb.row(btn(t, "btn_back", "pmgr", panel.id))
        await call.answer()
        await show(call, t("adm_pm_reset_help", mode=esc(renew_mode(None, panel, cfg))), kb.as_markup())
        return
    if action == "finance":
        await call.answer()
        await show(
            call,
            await financial_text(session, t, panel),
            InlineKeyboardMarkup(inline_keyboard=[[btn(t, "btn_back", "pmgr", panel.id)]]),
        )
        return
    if action == "plans":
        total = int(
            await session.scalar(
                select(func.count(Plan.id)).where(Plan.shop_id == shop.id, Plan.panel_id == panel.id)
            )
            or 0
        )
        pages = max(1, (total + PAGE - 1) // PAGE)
        page = min(max(0, callback_data.page), pages - 1)
        rows = list(
            await session.scalars(
                select(Plan)
                .where(Plan.shop_id == shop.id, Plan.panel_id == panel.id)
                .order_by(Plan.id)
                .offset(page * PAGE)
                .limit(PAGE)
            )
        )
        kb = InlineKeyboardBuilder()
        for plan in rows:
            kb.row(
                InlineKeyboardButton(
                    text=plan.title[:48], callback_data=Admin(section="plan", id=plan.id).pack()
                )
            )
        nav = []
        if page > 0:
            nav.append(btn(t, "adm_ud_previous", "pmgr", panel.id, "plans", page - 1))
        if page + 1 < pages:
            nav.append(btn(t, "adm_ud_next", "pmgr", panel.id, "plans", page + 1))
        if nav:
            kb.row(*nav)
        kb.row(btn(t, "btn_back", "pmgr", panel.id))
        await call.answer()
        await show(
            call,
            t("adm_pm_plans_head", name=esc(panel.name), total=total, page=page + 1, pages=pages),
            kb.as_markup(),
        )
        return
    if action != "show":
        await call.answer(t("button_expired"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await show(call, await panel_text(session, t, panel), management_kb(t, panel, staff))


async def financial_text(session: AsyncSession, t: Texts, panel: Panel) -> str:
    from datetime import timedelta

    services = select(Service.id).where(Service.shop_id == panel.shop_id, Service.panel_id == panel.id)
    base = [
        Order.shop_id == panel.shop_id,
        Order.service_id.in_(services),
        Order.status == OrderStatus.COMPLETED,
    ]
    since = utcnow() - timedelta(days=30)
    paid = [*base, Order.amount > 0, Order.kind != OrderKind.TRIAL]
    amount = int(await session.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(*paid)) or 0)
    last = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(*paid, Order.completed_at >= since)
        )
        or 0
    )
    trials = int(
        await session.scalar(select(func.count(Order.id)).where(*base, Order.kind == OrderKind.TRIAL)) or 0
    )
    lines = [
        t(
            "adm_pm_finance_head",
            name=esc(panel.name),
            amount=money(amount),
            last=money(last),
            average=money(last // 30),
            trials=trials,
        )
    ]
    for kind in (OrderKind.RENEW, OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS):
        num, total = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
                    *paid, Order.kind == kind, Order.completed_at >= since
                )
            )
        ).one()
        lines.append(
            t("adm_pm_finance_line", kind=t("adm_pm_order_" + kind.value), count=num, amount=money(total))
        )
    lines.append(t("adm_pm_finance_note"))
    return "\n\n".join(lines)


@router.message(PanelAdminStates.value, F.text, StaffOnly("panels.read"))
async def value_input(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
    cfg: dict[str, Any],
) -> None:
    data = await state.get_data()
    field = data.get("panel_field")
    if (
        not staff.can("panels.write")
        or data.get("panel_shop_id") != shop.id
        or field not in {"name", "capacity", "prefix"}
    ):
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    panel = await panel_owned(session, shop.id, int(data.get("panel_id") or 0), lock=True)
    if panel is None:
        await state.clear()
        await message.answer(t("button_expired"))
        return
    raw = (message.text or "").strip()
    value: str | int | None
    if field == "name":
        if not 1 <= len(raw) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
            await message.answer(t("adm_pm_invalid_name"))
            return
        column, value = "name", raw
    elif field == "capacity":
        number = 0 if raw == "-" else parse_int(raw)
        if number is None or not 0 <= number <= 1_000_000:
            await message.answer(t("adm_cfg_range", low=0, high=1_000_000))
            return
        column, value = "capacity", number or None
    else:
        prefix = "" if raw == "-" else raw.lower()
        if not re.fullmatch(r"[a-z0-9_]{0,16}", prefix):
            await message.answer(t("adm_cfg_invalid"))
            return
        column, value = "username_prefix", prefix
    before = {column: getattr(panel, column)}
    if before[column] != value:
        setattr(panel, column, value)
        await audit(session, shop, staff, panel, before, {column: value})
    else:
        await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(await panel_text(session, t, panel), reply_markup=management_kb(t, panel, staff))
