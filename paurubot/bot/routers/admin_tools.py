"""More staff tools in the bot (ADM-09): a customer's services, messages, free services, referrals
and reseller group; plans and prices; panels; money figures with an Excel export; settings;
a wallet gift for many customers; and broadcast options (audience, a link button, pinning)."""

from __future__ import annotations

import secrets
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor, back_home, customer_card
from paurubot.bot.routers.start import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import catalog, customers, finance, pricing
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import (
    Broadcast,
    Customer,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    ResellerGroup,
    ResellerProfile,
    Service,
    ServiceStatus,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.panels import GB, PanelError
from paurubot.provisioning import lifecycle
from paurubot.worker import queue

router = Router(name="admin_tools")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

PAGE = 8
STATUS_ICON = keyboards.STATUS_ICON
EXTEND = {"d7": (7, 0), "d30": (30, 0), "g10": (0, 10), "g50": (0, 50)}
# Settings staff can change from the bot, by group. Secrets and structured values stay on the web.
SETTINGS: dict[str, list[str]] = {
    "payments": [
        "min_topup",
        "max_topup",
        "card_enabled",
        "card_auto_approve",
        "card_auto_max_amount",
        "card_auto_min_history",
        "receipt_duplicate_block",
        "stars_enabled",
        "wallet_transfer_enabled",
    ],
    "services": [
        "extras_enabled",
        "extra_gb_price",
        "extra_day_price",
        "upgrade_enabled",
        "move_enabled",
        "move_price",
        "renew_resets_usage",
        "custom_buy_enabled",
    ],
    "growth": [
        "trial_enabled",
        "trial_days",
        "trial_gb",
        "referral_enabled",
        "referral_percent",
        "referral_daily_max",
        "start_gift_amount",
        "wheel_enabled",
        "lottery_enabled",
    ],
    "resellers": [
        "reseller_applications_enabled",
        "reseller_web_access",
        "bulk_buy_enabled",
        "panel_accounts_enabled",
    ],
    "timing": [
        "order_expire_minutes",
        "payment_expire_minutes",
        "usage_sync_minutes",
        "warn_days",
        "warn_remaining_percent",
        "delete_expired_after_days",
        "daily_report_hour",
    ],
}
SETTING_KEYS = [key for keys in SETTINGS.values() for key in keys]
BOUNDS: dict[str, tuple[int, int]] = {
    "referral_percent": (0, 100),
    "warn_remaining_percent": (0, 100),
    "daily_report_hour": (0, 23),
    "order_expire_minutes": (5, 7 * 24 * 60),
    "payment_expire_minutes": (5, 7 * 24 * 60),
    "usage_sync_minutes": (1, 24 * 60),
    "trial_days": (1, 365),
    "trial_gb": (1, 10_000),
    "card_auto_min_history": (0, 1_000),
}
AUDIENCES = ("all", "buyers", "active", "never_bought", "resellers")


def tools_kb(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    """Extra buttons for the admin home, by permission."""
    out: list[tuple[str, Admin]] = []
    if staff.can("catalog.write"):
        out.append((t("adm_plans"), Admin(section="plans")))
    if staff.can("panels.read"):
        out.append((t("adm_panels"), Admin(section="panels")))
    if staff.can("reports.read"):
        out.append((t("adm_finance"), Admin(section="finance")))
    if staff.can("settings.write"):
        out.append((t("adm_settings"), Admin(section="settings")))
    if staff.can("wallet.adjust"):
        out.append((t("adm_gift_all"), Admin(section="gift_all")))
    return out


async def _customer(session: AsyncSession, shop: ShopRow, customer_id: int) -> Customer | None:
    return await customers.get(session, shop.id, customer_id)


# --- a customer's services -----------------------------------------------------------------


def _service_line(service: Service) -> str:
    name = service.label or service.panel_username
    end = jdate(service.expire_at) if service.expire_at else "∞"
    return f"{STATUS_ICON.get(service.status, '')} {name} · {end}"[:64]


@router.callback_query(Admin.filter(F.section == "usvc"), StaffOnly("services.read"))
async def customer_services(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    rows = list(
        await session.scalars(
            select(Service)
            .where(Service.customer_id == customer.id, Service.status != ServiceStatus.DELETED)
            .order_by(Service.id.desc())
            .offset(callback_data.page * PAGE)
            .limit(PAGE + 1)
        )
    )
    await call.answer()
    kb = InlineKeyboardBuilder()
    for service in rows[:PAGE]:
        kb.button(text=_service_line(service), callback_data=Admin(section="svc", id=service.id))
    nav = []
    if callback_data.page:
        nav.append(("‹", Admin(section="usvc", id=customer.id, page=callback_data.page - 1)))
    if len(rows) > PAGE:
        nav.append(("›", Admin(section="usvc", id=customer.id, page=callback_data.page + 1)))
    for text, data in nav:
        kb.button(text=text, callback_data=data)
    kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id))
    kb.adjust(*([1] * len(rows[:PAGE])), max(1, len(nav)), 1)
    text = t("adm_user_services", name=esc(customer.display_name)) if rows else t("adm_no_services")
    await show(call, text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "user"), StaffOnly("customers.read"))
async def customer_again(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    await call.answer()
    text, kb = await customer_card(session, t, customer, staff)
    await show(call, text, kb)


async def _service(session: AsyncSession, shop: ShopRow, service_id: int) -> Service | None:
    service = await session.get(Service, service_id)
    if service is None or service.shop_id != shop.id or service.status == ServiceStatus.DELETED:
        return None
    return service


def service_text(t: Texts, service: Service) -> str:
    return t(
        "adm_service",
        name=esc(service.label or service.panel_username),
        username=esc(service.panel_username),
        status=t(f"status_{service.status.value}"),
        used=round(int(service.used_bytes) / GB, 2),
        limit=round(int(service.data_limit) / GB, 2) if service.data_limit else t("unlimited"),
        expires=jdate(service.expire_at) if service.expire_at else t("unlimited"),
    )


def service_kb(t: Texts, service: Service, staff: StaffIdentity) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if staff.can("services.write"):
        if service.status == ServiceStatus.DISABLED:
            kb.button(
                text=t("adm_svc_enable"), callback_data=Admin(section="svcact", action="on", id=service.id)
            )
        else:
            kb.button(
                text=t("adm_svc_disable"), callback_data=Admin(section="svcact", action="off", id=service.id)
            )
        for code in EXTEND:
            kb.button(
                text=t(f"adm_svc_{code}"), callback_data=Admin(section="svcact", action=code, id=service.id)
            )
        kb.button(
            text=t("adm_svc_delete"), callback_data=Admin(section="svcact", action="del", id=service.id)
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="usvc", id=service.customer_id))
    kb.adjust(1, 2, 2, 1, 1)
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == "svc"), StaffOnly("services.read"))
async def service_card(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    service = await _service(session, shop, callback_data.id)
    if service is None:
        await call.answer(t("adm_service_not_found"), show_alert=True)
        return
    await call.answer()
    await show(call, service_text(t, service), service_kb(t, service, staff))


@router.callback_query(Admin.filter(F.section == "svcact"), StaffOnly("services.write"))
async def service_action(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    service = await _service(session, shop, callback_data.id)
    if service is None:
        await call.answer(t("adm_service_not_found"), show_alert=True)
        return
    action = callback_data.action
    if action == "del" and not callback_data.page:
        await call.answer()
        await show(
            call,
            t("adm_svc_delete_confirm", name=esc(service.label or service.panel_username)),
            keyboards.confirm(
                t,
                Admin(section="svcact", action="del", id=service.id, page=1).pack(),
                Admin(section="svc", id=service.id).pack(),
            ),
        )
        return
    try:
        if action in {"on", "off"}:
            await lifecycle.set_enabled(session, service, action == "on", "staff")
        elif action in EXTEND:
            days, gb = EXTEND[action]
            await lifecycle.extend(session, service, days=days, gb=gb)
        elif action == "del":
            await lifecycle.delete_service(session, service, "deleted by staff")
        else:
            await call.answer()
            return
    except PanelError:
        await call.answer(t("adm_panel_error"), show_alert=True)
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action=f"service.bot_{action}",
        entity="service",
        entity_id=service.id,
    )
    alert = {"off": "disabled_by_staff", "del": "removed_by_staff"}.get(action)
    if alert:
        await queue.enqueue(
            session,
            "notify",
            "service_alert",
            {"service_id": service.id, "alert": alert, "reason": ""},
            shop_id=shop.id,
        )
    await session.commit()
    await call.answer(t("adm_saved"))
    if action == "del":
        await show(
            call,
            t("adm_svc_deleted"),
            keyboards.back(t, Admin(section="usvc", id=service.customer_id).pack()),
        )
        return
    await show(call, service_text(t, service), service_kb(t, service, staff))


# --- message, free service, referrals, reseller group ----------------------------------------


@router.callback_query(Admin.filter(F.section == "umsg"), StaffOnly("customers.write"))
async def message_ask(call: CallbackQuery, callback_data: Admin, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.message)
    await state.update_data(customer_id=callback_data.id)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_message_ask"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.message, F.text)
async def message_send(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    from paurubot.notifications import telegram

    data = await state.get_data()
    await state.clear()
    customer = await _customer(session, shop, int(data.get("customer_id") or 0))
    if customer is None:
        await message.answer(t("adm_customer_not_found"), reply_markup=menu(t, cfg, staff))
        return
    try:
        await telegram.send_text(
            customer.telegram_id, t("adm_message_from_shop", text=esc(message.text or "")), shop_id=shop.id
        )
    except telegram.Undeliverable:
        await message.answer(t("adm_message_undeliverable"), reply_markup=menu(t, cfg, staff))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.message",
        entity="customer",
        entity_id=customer.id,
    )
    await message.answer(t("adm_message_sent"), reply_markup=menu(t, cfg, staff))


@router.callback_query(Admin.filter(F.section == "ugive"), StaffOnly("services.create"))
async def give_service(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """A free service for this customer from any plan, through the normal provisioning queue."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    if callback_data.action != "plan":
        await call.answer()
        kb = InlineKeyboardBuilder()
        for option in await catalog.plans(session, shop.id, None):
            if option.delivery == "stock":
                continue
            kb.button(
                text=f"{option.title} — {money(option.price)}"[:64],
                callback_data=Admin(section="ugive", action="plan", id=customer.id, page=option.id),
            )
        kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id))
        kb.adjust(1)
        await show(call, t("adm_give_pick"), kb.as_markup())
        return
    plan = await session.get(Plan, callback_data.page)
    if plan is None or plan.shop_id != shop.id or not plan.is_active:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    from paurubot.db.base import utcnow

    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        plan_id=plan.id,
        params={},
        base_price=plan.price,
        amount=0,
        paid_at=utcnow(),
        source="staff_bot",
    )
    session.add(order)
    await session.flush()
    await queue.enqueue(
        session,
        "provision",
        "order",
        {"order_id": order.id},
        shop_id=shop.id,
        dedupe_key=f"order:{order.id}:provision",
    )
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="service.create_manual",
        entity="order",
        entity_id=order.id,
        after={"customer_id": customer.id, "plan_id": plan.id},
    )
    await session.commit()
    await call.answer(t("adm_given"))
    await show(call, t("adm_given"), keyboards.back(t, Admin(section="user", id=customer.id).pack()))


@router.callback_query(Admin.filter(F.section == "uref"), StaffOnly("customers.read"))
async def referrals(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    rows = list(
        await session.scalars(
            select(Customer).where(Customer.referrer_id == customer.id).order_by(Customer.id.desc()).limit(15)
        )
    )
    total = await customers.referral_count(session, customer.id)
    await call.answer()
    lines = [t("adm_referrals", name=esc(customer.display_name), count=total)]
    lines += [f"• {esc(r.display_name)} — <code>{r.telegram_id}</code>" for r in rows]
    await show(call, "\n".join(lines), keyboards.back(t, Admin(section="user", id=customer.id).pack()))


@router.callback_query(Admin.filter(F.section == "ugroup"), StaffOnly("resellers.manage"))
async def reseller_group(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    profile = (
        await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
        if customer
        else None
    )
    if customer is None or profile is None or profile.parent_id is not None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    groups = list(
        await session.scalars(
            select(ResellerGroup)
            .where(ResellerGroup.shop_id == shop.id, ResellerGroup.is_active.is_(True))
            .order_by(ResellerGroup.id)
        )
    )
    if callback_data.action == "set":
        if callback_data.page not in {g.id for g in groups}:
            await call.answer(t("adm_no_groups"), show_alert=True)
            return
        before = profile.group_id
        profile.group_id = callback_data.page
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="reseller.group",
            entity="reseller_profile",
            entity_id=profile.id,
            before={"group_id": before},
            after={"group_id": profile.group_id},
        )
        await session.commit()
        await call.answer(t("adm_saved"))
        text, kb = await customer_card(session, t, customer, staff)
        await show(call, text, kb)
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    for group in groups:
        mark = "✅ " if group.id == profile.group_id else ""
        kb.button(
            text=f"{mark}{group.title} ({group.discount_percent}%)",
            callback_data=Admin(section="ugroup", action="set", id=customer.id, page=group.id),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id))
    kb.adjust(1)
    await show(call, t("adm_choose_group") if groups else t("adm_no_groups"), kb.as_markup())


# --- plans and prices ----------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "plans"), StaffOnly("catalog.write"))
async def plans_list(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    rows = list(
        await session.scalars(
            select(Plan)
            .where(Plan.shop_id == shop.id)
            .order_by(Plan.is_active.desc(), Plan.sort, Plan.id)
            .offset(callback_data.page * PAGE)
            .limit(PAGE + 1)
        )
    )
    await call.answer()
    kb = InlineKeyboardBuilder()
    for plan in rows[:PAGE]:
        mark = "🟢" if plan.is_active else "⚪️"
        kb.button(
            text=f"{mark} {plan.title} — {money(plan.price)}"[:64],
            callback_data=Admin(section="plan", id=plan.id),
        )
    if callback_data.page:
        kb.button(text="‹", callback_data=Admin(section="plans", page=callback_data.page - 1))
    if len(rows) > PAGE:
        kb.button(text="›", callback_data=Admin(section="plans", page=callback_data.page + 1))
    kb.button(text=t("adm_bulk_price"), callback_data=Admin(section="pbulk"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(1)
    await show(call, t("adm_plans_title"), kb.as_markup())


def plan_kb(t: Texts, plan: Plan) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("adm_plan_hide") if plan.is_active else t("adm_plan_show"),
        callback_data=Admin(section="plan", action="toggle", id=plan.id),
    )
    kb.button(text=t("adm_plan_price"), callback_data=Admin(section="plan", action="price", id=plan.id))
    kb.button(text=t("btn_back"), callback_data=Admin(section="plans"))
    kb.adjust(2, 1)
    return kb.as_markup()


def plan_text(t: Texts, plan: Plan) -> str:
    return t(
        "adm_plan",
        title=esc(plan.title),
        price=money(plan.price),
        days=plan.days or t("unlimited"),
        gb=plan.data_gb or t("unlimited"),
        state=t("adm_plan_on") if plan.is_active else t("adm_plan_off"),
    )


@router.callback_query(Admin.filter(F.section == "plan"), StaffOnly("catalog.write"))
async def plan_card(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    plan = await session.get(Plan, callback_data.id)
    if plan is None or plan.shop_id != shop.id:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    if callback_data.action == "toggle":
        plan.is_active = not plan.is_active
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="plan.toggle",
            entity="plan",
            entity_id=plan.id,
            after={"is_active": plan.is_active},
        )
        await session.commit()
    elif callback_data.action == "price":
        await call.answer()
        await state.set_state(AdminStates.plan_price)
        await state.update_data(plan_id=plan.id)
        if isinstance(call.message, Message):
            await call.message.answer(
                t("adm_plan_price_ask", price=money(plan.price)), reply_markup=keyboards.cancel(t)
            )
        return
    await call.answer()
    await show(call, plan_text(t, plan), plan_kb(t, plan))


@router.message(AdminStates.plan_price, F.text)
async def plan_price(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    value = parse_int(message.text)
    if value is None or value < 0 or value > 10**12:
        await message.answer(t("number_expected"))
        return
    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, int(data.get("plan_id") or 0))
    if plan is None or plan.shop_id != shop.id:
        await message.answer(t("plan_unavailable"), reply_markup=menu(t, cfg, staff))
        return
    before = plan.price
    plan.price = value
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="plan.price",
        entity="plan",
        entity_id=plan.id,
        before={"price": before},
        after={"price": value},
    )
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(plan_text(t, plan), reply_markup=plan_kb(t, plan))


@router.callback_query(Admin.filter(F.section == "pbulk"), StaffOnly("catalog.write"))
async def bulk_price_ask(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.bulk_price)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_bulk_price_ask"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.bulk_price, F.text)
async def bulk_price(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """Every active plan's price up or down by a percentage, rounded like other prices."""
    text = (message.text or "").strip().replace("%", "").replace("\u066a", "")
    sign = -1 if text.startswith(("-", "−")) else 1
    percent = parse_int(text.lstrip("+-−"))
    if percent is None or not 1 <= percent <= 90:
        await message.answer(t("adm_bulk_price_invalid"))
        return
    await state.clear()
    plans = list(await session.scalars(select(Plan).where(Plan.shop_id == shop.id, Plan.is_active.is_(True))))
    for plan in plans:
        plan.price = pricing.round_price(plan.price * (100 + sign * percent) / 100)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="plan.bulk_price",
        entity="plan",
        after={"percent": sign * percent, "plans": len(plans)},
    )
    await session.commit()
    await message.answer(
        t("adm_bulk_price_done", count=len(plans), percent=sign * percent), reply_markup=menu(t, cfg, staff)
    )


# --- panels --------------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "panels"), StaffOnly("panels.read"))
async def panels_list(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    if callback_data.action == "toggle":
        if not staff.can("panels.write"):
            await call.answer(t("adm_forbidden"), show_alert=True)
            return
        panel = await session.get(Panel, callback_data.id)
        if panel is None or panel.shop_id != shop.id:
            await call.answer()
            return
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
    rows = list(await session.scalars(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id)))
    counts = dict(
        (
            await session.execute(
                select(Service.panel_id, func.count(Service.id))
                .where(Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE)
                .group_by(Service.panel_id)
            )
        ).all()
    )
    await call.answer()
    lines = [t("adm_panels_title")]
    kb = InlineKeyboardBuilder()
    for panel in rows:
        health = (
            "⚪️"
            if not panel.is_active
            else ("🟢" if panel.is_healthy else ("🔴" if panel.is_healthy is False else "🟡"))
        )
        lines.append(
            t(
                "adm_panel_line",
                icon=health,
                name=esc(panel.name),
                kind=panel.kind.value,
                active=counts.get(panel.id, 0),
            )
        )
        if staff.can("panels.write"):
            kb.button(
                text=(t("adm_panel_off") if panel.is_active else t("adm_panel_on")) + f" {panel.name}"[:48],
                callback_data=Admin(section="panels", action="toggle", id=panel.id),
            )
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


# --- money ---------------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "finance"), StaffOnly("reports.read"))
async def finance_view(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    if callback_data.action == "xlsx":
        await call.answer(t("adm_export_wait"))
        data = await finance.orders_xlsx(session, shop.id, callback_data.page or 30)
        if isinstance(call.message, Message):
            await call.message.answer_document(
                BufferedInputFile(data, filename=f"orders-{callback_data.page or 30}d.xlsx"),
                caption=t("adm_export_caption", days=callback_data.page or 30),
            )
        return
    periods, active = await finance.summary(session, shop.id)
    await call.answer()
    lines = [t("adm_finance_title")]
    for p in periods:
        lines.append(
            t(
                f"adm_finance_{p.key}",
                sales=p.sales,
                sales_amount=money(p.sales_amount),
                topups=p.topups,
                topup_amount=money(p.topup_amount),
                joined=p.new_customers,
            )
        )
    lines.append(t("adm_finance_active", active=active))
    kb = InlineKeyboardBuilder()
    for days in (7, 30, 90):
        kb.button(
            text=t("adm_export_days", days=days),
            callback_data=Admin(section="finance", action="xlsx", page=days),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(3, 1)
    await show(call, "\n\n".join(lines), kb.as_markup())


# --- settings ------------------------------------------------------------------------------


def _value_text(t: Texts, value: Any) -> str:
    if isinstance(value, bool):
        return "✅" if value else "❌"
    if value in (None, ""):
        return "—"
    return esc(str(value))


@router.callback_query(Admin.filter(F.section == "settings"), StaffOnly("settings.write"))
async def settings_view(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    groups = list(SETTINGS)
    if callback_data.action == "show":
        await call.answer()
        kb = InlineKeyboardBuilder()
        for index, group in enumerate(groups):
            kb.button(
                text=t(f"adm_set_group_{group}"),
                callback_data=Admin(section="settings", action="group", page=index),
            )
        kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
        kb.adjust(2)
        await show(call, t("adm_settings_title"), kb.as_markup())
        return
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    if callback_data.action in {"toggle", "ask"}:
        if not 0 <= callback_data.id < len(SETTING_KEYS):
            await call.answer()
            return
        key = SETTING_KEYS[callback_data.id]
        group_index = next(i for i, g in enumerate(groups) if key in SETTINGS[g])
        if callback_data.action == "ask":
            await call.answer()
            await state.set_state(AdminStates.setting_value)
            await state.update_data(setting=key, group=group_index)
            if isinstance(call.message, Message):
                await call.message.answer(
                    t("adm_setting_ask", name=t(f"adm_set_{key}"), value=_value_text(t, cfg.get(key))),
                    reply_markup=keyboards.cancel(t),
                )
            return
        await _save_setting(session, shop, staff, key, not bool(cfg.get(key)))
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        callback_data = Admin(section="settings", action="group", page=group_index)
    if not 0 <= callback_data.page < len(groups):
        await call.answer()
        return
    await call.answer()
    group = groups[callback_data.page]
    await show(call, t(f"adm_set_group_{group}"), _group_kb(t, group, cfg))


def _group_kb(t: Texts, group: str, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in SETTINGS[group]:
        value = cfg.get(key)
        action = "toggle" if isinstance(value, bool) or shop_settings.SPECS[key].kind is bool else "ask"
        kb.button(
            text=f"{t(f'adm_set_{key}')}: {_value_text(t, value)}"[:64],
            callback_data=Admin(section="settings", action=action, id=SETTING_KEYS.index(key)),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="settings"))
    kb.adjust(1)
    return kb.as_markup()


async def _save_setting(
    session: AsyncSession, shop: ShopRow, staff: StaffIdentity, key: str, value: Any
) -> None:
    before = await shop_settings.get(session, shop.id, key)
    await shop_settings.set_many(session, shop.id, {key: value})
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.update",
        entity="shop",
        entity_id=shop.id,
        before={key: before},
        after={key: value},
    )
    await session.commit()


@router.message(AdminStates.setting_value, F.text)
async def setting_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    key = str(data.get("setting") or "")
    if key not in SETTING_KEYS:
        await state.clear()
        return
    spec = shop_settings.SPECS[key]
    raw: Any = (message.text or "").strip()
    if spec.kind is int:
        raw = parse_int(raw)
        low, high = BOUNDS.get(key, (0, 10**12))
        if raw is None or not low <= raw <= high:
            await message.answer(t("adm_setting_range", low=low, high=high))
            return
    elif len(raw) > 255:
        await message.answer(t("adm_setting_invalid"))
        return
    try:
        value = shop_settings.coerce(key, raw)
    except shop_settings.SettingError:
        await message.answer(t("adm_setting_invalid"))
        return
    await state.clear()
    await _save_setting(session, shop, staff, key, value)
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    group = list(SETTINGS)[int(data.get("group") or 0)]
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(t(f"adm_set_group_{group}"), reply_markup=_group_kb(t, group, cfg))


# --- a wallet gift for many customers ------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "gift_all"), StaffOnly("wallet.adjust"))
async def gift_all(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    amount = int(data.get("gift_amount") or 0)
    if callback_data.action == "show" or amount <= 0:
        await call.answer()
        await state.set_state(AdminStates.gift_amount)
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_gift_amount_ask"), reply_markup=keyboards.cancel(t))
        return
    if callback_data.action == "aud":
        await call.answer()
        audience = AUDIENCES[callback_data.page % len(AUDIENCES)]
        count = await _audience_count(session, shop.id, audience)
        await state.update_data(gift_audience=audience)
        await show(
            call,
            t("adm_gift_confirm", amount=money(amount), count=count, audience=t(f"adm_aud_{audience}")),
            keyboards.confirm(t, Admin(section="gift_all", action="go").pack(), Admin(section="home").pack()),
        )
        return
    if callback_data.action == "go":
        audience = str(data.get("gift_audience") or "")
        if audience not in AUDIENCES:
            await call.answer()
            return
        await state.clear()
        batch = secrets.token_hex(6)
        await queue.enqueue(
            session,
            "broadcast",
            "gift",
            {"batch": batch, "amount": amount, "audience": audience, "cursor": 0},
            shop_id=shop.id,
            dedupe_key=f"gift:{batch}",
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="wallet.gift_all",
            entity="shop",
            entity_id=shop.id,
            after={"amount": amount, "audience": audience, "batch": batch},
        )
        await session.commit()
        await call.answer(t("adm_gift_queued"))
        await show(call, t("adm_gift_queued"), back_home(t))


@router.message(AdminStates.gift_amount, F.text)
async def gift_amount(message: Message, t: Texts, state: FSMContext) -> None:
    amount = parse_int(message.text)
    if amount is None or not 1_000 <= amount <= 10_000_000:
        await message.answer(t("adm_gift_amount_invalid"))
        return
    await state.set_state(None)
    await state.update_data(gift_amount=amount)
    await message.answer(
        t("adm_gift_audience", amount=money(amount)), reply_markup=audience_kb(t, "gift_all")
    )


def audience_kb(t: Texts, section: str, row_id: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, audience in enumerate(AUDIENCES):
        kb.button(
            text=t(f"adm_aud_{audience}"),
            callback_data=Admin(section=section, action="aud", id=row_id, page=index),
        )
    kb.button(text=t("btn_cancel"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


async def _audience_count(session: AsyncSession, shop_id: int, audience: str) -> int:
    from paurubot.notifications.handlers import audience_conditions

    return int(
        await session.scalar(select(func.count(Customer.id)).where(*audience_conditions(shop_id, audience)))
        or 0
    )


# --- broadcast options ---------------------------------------------------------------------


def broadcast_kb(t: Texts, row: Broadcast) -> InlineKeyboardMarkup:
    audience = (row.audience or {}).get("kind", "all")
    pin = bool((row.audience or {}).get("pin"))
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("adm_bc_audience", audience=t(f"adm_aud_{audience}")),
        callback_data=Admin(section="bc", action="aud_menu", id=row.id),
    )
    kb.button(
        text=t("adm_bc_pin_on") if pin else t("adm_bc_pin_off"),
        callback_data=Admin(section="bc", action="pin", id=row.id),
    )
    kb.button(
        text=t("adm_bc_button_set", text=esc(row.buttons[0]["text"]))
        if row.buttons
        else t("adm_bc_button_add"),
        callback_data=Admin(section="bc", action="button", id=row.id),
    )
    kb.button(text=t("adm_bc_send"), callback_data=Admin(section="bc", action="send", id=row.id))
    kb.button(text=t("btn_cancel"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


async def broadcast_options_text(session: AsyncSession, t: Texts, row: Broadcast) -> str:
    audience = (row.audience or {}).get("kind", "all")
    return t("adm_bc_options", count=await _audience_count(session, row.shop_id, audience))


@router.callback_query(Admin.filter(F.section == "bc"), StaffOnly("broadcast.send"))
async def broadcast_options(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    row = await session.get(Broadcast, callback_data.id)
    if row is None or row.shop_id != shop.id or row.status != JobStatus.DRAFT:
        await call.answer(t("adm_application_done"), show_alert=True)
        return
    action = callback_data.action
    if action == "aud_menu":
        await call.answer()
        await show(call, t("adm_bc_pick_audience"), audience_kb(t, "bc", row.id))
        return
    if action == "aud":
        row.audience = {**(row.audience or {}), "kind": AUDIENCES[callback_data.page % len(AUDIENCES)]}
    elif action == "pin":
        row.audience = {**(row.audience or {}), "pin": not (row.audience or {}).get("pin")}
    elif action == "button":
        await call.answer()
        await state.set_state(AdminStates.broadcast_button)
        await state.update_data(broadcast_id=row.id)
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_bc_button_ask"), reply_markup=keyboards.cancel(t))
        return
    elif action == "send":
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
            after={"audience": row.audience, "buttons": len(row.buttons or [])},
        )
        await session.commit()
        await call.answer(t("adm_broadcast_queued"))
        await show(call, t("adm_broadcast_queued"), back_home(t))
        return
    await session.commit()
    await call.answer()
    await show(call, await broadcast_options_text(session, t, row), broadcast_kb(t, row))


@router.message(AdminStates.broadcast_button, F.text)
async def broadcast_button(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """`Text | https://link` adds one link button under the message; `-` removes it."""
    import re

    data = await state.get_data()
    row = await session.get(Broadcast, int(data.get("broadcast_id") or 0))
    if row is None or row.shop_id != shop.id or row.status != JobStatus.DRAFT:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "-":
        row.buttons = []
    else:
        text, _, url = raw.partition("|")
        text, url = text.strip()[:64], url.strip()
        if not text or not re.fullmatch(r"(https://|tg://)\S{3,500}", url):
            await message.answer(t("adm_bc_button_invalid"))
            return
        row.buttons = [{"text": text, "url": url}]
    await state.clear()
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(await broadcast_options_text(session, t, row), reply_markup=broadcast_kb(t, row))
