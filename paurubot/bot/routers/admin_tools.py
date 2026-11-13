"""More staff tools in the bot (ADM-09): a customer's services, messages, free services, referrals
and reseller group; plans and prices; panels; money figures with an Excel export; settings;
a wallet gift for many customers; and broadcast options (audience, a link button, pinning)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor, back_home, customer_card
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, clear_customer_input, show
from paurubot.core import catalog, confirm, customers, finance, pricing
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
REFERRAL_PAGE = 10
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
        "custom_username_enabled",
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
        "reseller_bot_open",
        "resale_trial_fee",
    ],
    "timing": [
        "order_expire_minutes",
        "payment_expire_minutes",
        "usage_sync_minutes",
        "warn_days",
        "warn_remaining_percent",
        "delete_expired_after_days",
        "delete_warn_days",
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
    "delete_warn_days": (0, 30),
    "trial_days": (1, 365),
    "trial_gb": (1, 10_000),
    "card_auto_min_history": (0, 1_000),
    "resale_trial_fee": (0, 10_000_000),
}
AUDIENCES = ("all", "buyers", "active", "inactive", "never_bought", "resellers")
PIN_CYCLE = (None, 0, 24, 48)  # off, until unpinned, 24 h, 48 h
GIVE_GUARD_SECONDS = 30  # the same free service again within this window is a double tap


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
        out.append((t("adm_look"), Admin(section="look")))
    if staff.can("wallet.adjust"):
        out.append((t("adm_gift_all"), Admin(section="gift_all")))
    if staff.can("broadcast.send"):
        out.append((t("adm_bc_unpin"), Admin(section="bc_unpin")))
    return out


async def _customer(session: AsyncSession, shop: ShopRow, customer_id: int) -> Customer | None:
    return await customers.get(session, shop.id, customer_id)


@router.callback_query(Admin.filter(F.section == "utransfer"), StaffOnly("resellers.manage"))
async def customer_transfer_menu(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.core import customer_transfer

    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    targets = await customer_transfer.target_shops(session, shop.id)
    if callback_data.action == "move":
        target = next((row for row in targets if row.id == callback_data.page), None)
        if target is None:
            await call.answer(t("adm_transfer_target_invalid"), show_alert=True)
            return
        try:
            result = await customer_transfer.transfer_customer(session, customer.id, target.id)
        except customer_transfer.CustomerTransferError as exc:
            await call.answer(t(f"adm_transfer_{exc.key}"), show_alert=True)
            return
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="customer.transfer",
            entity="customer",
            entity_id=result.customer_id,
            before={"shop_id": result.source_shop_id},
            after={"shop_id": result.target_shop_id},
        )
        await session.commit()
        await call.answer(t("adm_transfer_done"))
        await show(call, t("adm_transfer_done"), keyboards.back(t, Admin(section="home").pack()))
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    for target in targets:
        kb.row(
            InlineKeyboardButton(
                text=t("adm_transfer_target", title=esc(target.title)),
                callback_data=Admin(
                    section="utransfer", action="move", id=customer.id, page=target.id
                ).pack(),
            )
        )
    kb.row(
        InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id).pack())
    )
    await show(call, t("adm_transfer_choose") if targets else t("adm_transfer_no_targets"), kb.as_markup())


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
    state: FSMContext,
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    if callback_data.action.startswith(("uf", "lf")):
        from paurubot.bot.routers.admin_hub import USER_FILTERS

        suffix = callback_data.action[2:]
        if not suffix.isdecimal() or not 0 <= int(suffix) < len(USER_FILTERS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        await state.clear()
        await state.update_data(
            user_list_origin={
                "shop_id": shop.id,
                "customer_id": customer.id,
                "filter": int(suffix),
                "page": max(0, callback_data.page),
                "action": "uf" if callback_data.action.startswith("uf") else "f",
            }
        )
    await call.answer()
    text, kb = await customer_card(session, t, customer, staff)
    await show(call, text, kb)


async def _service(session: AsyncSession, shop: ShopRow, service_id: int) -> Service | None:
    service = await session.get(Service, service_id)
    if service is None or service.shop_id != shop.id or service.status == ServiceStatus.DELETED:
        return None
    return service


def service_text(t: Texts, service: Service) -> str:
    note = "\n" + t("adm_service_note", note=esc(service.staff_note)) if service.staff_note else ""
    return _service_body(t, service) + note


def _service_body(t: Texts, service: Service) -> str:
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
        kb.button(text=t("adm_svc_note"), callback_data=Admin(section="svcnote", id=service.id))
    kb.button(text=t("btn_back"), callback_data=Admin(section="usvc", id=service.customer_id))
    kb.adjust(1, 2, 2, 2, 1)
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
        # Two steps (F-22): the confirm button carries a one-time number tied to this staff member
        # and service, so an old or forwarded button can't delete anything.
        nonce = await confirm.issue_nonce(f"tg:{staff.id}", "service.delete", service.id)
        await call.answer()
        await show(
            call,
            t("adm_svc_delete_confirm", name=esc(service.label or service.panel_username)),
            keyboards.confirm(
                t,
                Admin(section="svcact", action="del", id=service.id, page=nonce).pack(),
                Admin(section="svc", id=service.id).pack(),
            ),
        )
        return
    if action == "del" and not await confirm.consume_nonce(
        f"tg:{staff.id}", "service.delete", service.id, callback_data.page
    ):
        await call.answer(t("button_expired"), show_alert=True)
        return
    if action not in {"on", "off", "del"} and action not in EXTEND:
        await call.answer()
        return
    try:
        # One owner per service: never write over a renewal or sync in flight (SEC-F09).
        async with lifecycle.exclusive(session, service):
            if action in {"on", "off"}:
                await lifecycle.set_enabled(session, service, action == "on", "staff")
            elif action in EXTEND:
                days, gb = EXTEND[action]
                await lifecycle.extend(session, service, days=days, gb=gb)
            else:
                await lifecycle.delete_service(session, service, "deleted by staff")
    except lifecycle.ServiceBusy:
        await session.rollback()
        await call.answer(t("service_busy"), show_alert=True)
        return
    except PanelError:
        # The update middleware commits after the handler: drop whatever was half changed.
        await session.rollback()
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


MESSAGE_MEDIA = F.text | F.photo | F.video | F.document | F.animation | F.voice | F.audio


@router.message(AdminStates.message, MESSAGE_MEDIA)
async def message_send(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """Text or media for one customer (Foxima parity): staff then choose whether it carries a
    reply button, which opens the customer's support tickets."""
    data = await state.get_data()
    customer = await _customer(session, shop, int(data.get("customer_id") or 0))
    await clear_customer_input(state)
    if customer is None:
        await message.answer(t("adm_customer_not_found"), reply_markup=menu(t, cfg, staff))
        return
    await state.update_data(
        umsg={
            "customer_id": customer.id,
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "text": message.text if message.text else None,
            "caption": message.caption or "",
        }
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("adm_umsg_reply_yes"), callback_data=Admin(section="umsgs", action="reply", id=customer.id)
    )
    kb.button(
        text=t("adm_umsg_reply_no"), callback_data=Admin(section="umsgs", action="plain", id=customer.id)
    )
    kb.button(text=t("btn_cancel"), callback_data=Admin(section="user", id=customer.id))
    kb.adjust(1)
    await message.answer(t("adm_umsg_reply_ask"), reply_markup=kb.as_markup())


@router.callback_query(Admin.filter(F.section == "umsgs"), StaffOnly("customers.write"))
async def message_deliver(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from paurubot.bot.callbacks import Menu
    from paurubot.notifications import telegram

    data = (await state.get_data()).get("umsg") or {}
    customer = await _customer(session, shop, callback_data.id)
    if customer is None or int(data.get("customer_id") or 0) != customer.id:
        await call.answer(t("button_expired"), show_alert=True)
        return
    await state.update_data(umsg=None)
    await call.answer()
    markup = None
    if callback_data.action == "reply":
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("btn_reply_staff"), callback_data=Menu(item="tickets").pack())]
            ]
        )
    try:
        if data.get("text"):
            await telegram.send_text(
                customer.telegram_id,
                t("adm_message_from_shop", text=esc(str(data["text"]))),
                shop_id=shop.id,
                reply_markup=markup,
            )
        else:
            caption = str(t("adm_message_from_shop", text=esc(str(data.get("caption") or ""))))[:1024]
            bot = call.bot
            assert bot is not None
            await telegram.deliver(
                lambda: bot.copy_message(
                    customer.telegram_id,
                    int(data["chat_id"]),
                    int(data["message_id"]),
                    caption=caption,
                    reply_markup=markup,
                )
            )
    except telegram.Undeliverable:
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_message_undeliverable"), reply_markup=menu(t, cfg, staff))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.message",
        entity="customer",
        entity_id=customer.id,
        after={"media": not data.get("text"), "reply": markup is not None},
    )
    await session.commit()
    if isinstance(call.message, Message):  # and the admin keyboard back in place of "cancel"
        await call.message.answer(t("adm_message_sent"), reply_markup=menu(t, cfg, staff))


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

    # A double tap (or Telegram resending a slow callback) must not give two services: the row
    # lock makes the second tap wait for the first one's commit, then it sees that order.
    await session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
    recent = await session.scalar(
        select(Order.id)
        .where(
            Order.customer_id == customer.id,
            Order.plan_id == plan.id,
            Order.source == "staff_bot",
            Order.paid_at >= utcnow() - timedelta(seconds=GIVE_GUARD_SECONDS),
        )
        .limit(1)
    )
    if recent is not None:
        await call.answer(t("adm_given"))
        return
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
    conds = [Customer.shop_id == shop.id, Customer.referrer_id == customer.id]
    total = int(await session.scalar(select(func.count(Customer.id)).where(*conds)) or 0)
    pages = max(1, (total + REFERRAL_PAGE - 1) // REFERRAL_PAGE)
    page = min(max(0, callback_data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(Customer)
            .where(*conds)
            .order_by(Customer.id.desc())
            .offset(page * REFERRAL_PAGE)
            .limit(REFERRAL_PAGE)
        )
    )
    await call.answer()
    lines = [t("adm_referrals", name=esc(customer.display_name), count=total)]
    lines += [f"• {esc(r.display_name)} — <code>{r.telegram_id}</code>" for r in rows]
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_previous"),
                callback_data=Admin(section="uref", id=customer.id, page=page - 1).pack(),
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_next"),
                callback_data=Admin(section="uref", id=customer.id, page=page + 1).pack(),
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id).pack())
    )
    await show(call, "\n".join(lines), kb.as_markup())


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


# --- reseller sales cap -------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "ulimit"), StaffOnly("resellers.manage"))
async def reseller_limit_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    profile = (
        await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
        if customer
        else None
    )
    if customer is None or profile is None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminStates.reseller_limit)
    await state.update_data(customer_id=customer.id)
    if isinstance(call.message, Message):
        await call.message.answer(
            t(
                "adm_reseller_limit_ask",
                current=profile.max_purchases_per_day or t("adm_reseller_limit_unlimited"),
            ),
            reply_markup=keyboards.cancel(t),
        )


@router.message(AdminStates.reseller_limit, F.text, StaffOnly("resellers.manage"))
async def reseller_limit_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    value = parse_int(message.text)
    if value is None or value < 0 or value > 100_000:
        await message.answer(t("adm_reseller_limit_invalid"))
        return
    data = await state.get_data()
    customer = await _customer(session, shop, int(data.get("customer_id") or 0))
    profile = (
        await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
        if customer
        else None
    )
    if customer is None or profile is None:
        await state.clear()
        await message.answer(t("adm_not_reseller"), reply_markup=menu(t, cfg, staff))
        return
    before = profile.max_purchases_per_day
    profile.max_purchases_per_day = value or None
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="reseller.daily_limit",
        entity="reseller_profile",
        entity_id=profile.id,
        before={"max_purchases_per_day": before},
        after={"max_purchases_per_day": profile.max_purchases_per_day},
    )
    await session.commit()
    await state.clear()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    text, kb = await customer_card(session, t, customer, staff)
    await message.answer(text, reply_markup=kb)


# --- plans and prices ----------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "plans"), StaffOnly("catalog.write"))
async def plans_list(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    total = int(await session.scalar(select(func.count(Plan.id)).where(Plan.shop_id == shop.id)) or 0)
    page_size = 10
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(0, callback_data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(Plan)
            .where(Plan.shop_id == shop.id)
            .order_by(Plan.is_active.desc(), Plan.sort, Plan.id)
            .offset(page * page_size)
            .limit(page_size)
        )
    )
    await call.answer()
    kb = InlineKeyboardBuilder()
    for plan in rows:
        mark = "🟢" if plan.is_active else "⚪️"
        kb.button(
            text=f"{mark} {plan.title} — {money(plan.price)}"[:64],
            callback_data=Admin(section="pedit", id=plan.id, action="show"),
        )
    if page:
        kb.button(text="‹", callback_data=Admin(section="plans", page=page - 1))
    if page + 1 < pages:
        kb.button(text="›", callback_data=Admin(section="plans", page=page + 1))
    kb.button(text=t("adm_bulk_price"), callback_data=Admin(section="pbulk"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="nav", action="shop"))
    kb.adjust(1)
    await show(call, t("adm_s_products_head", total=total, page=page + 1, pages=pages), kb.as_markup())


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
    if callback_data.action in {"", "show"}:
        from paurubot.bot.routers.admin_shop import show_product

        await state.clear()
        await call.answer()
        await show_product(call, session, t, plan)
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
    total = int(await session.scalar(select(func.count(Panel.id)).where(Panel.shop_id == shop.id)) or 0)
    pages = max(1, (total + 9) // 10)
    page = min(max(0, callback_data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id).offset(page * 10).limit(10)
        )
    )
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
    lines = [t("adm_pm_list_head", total=total, page=page + 1, pages=pages)]
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
        kb.row(
            InlineKeyboardButton(
                text=t("adm_pm_manage_name", name=panel.name[:40]),
                callback_data=Admin(section="pmgr", id=panel.id).pack(),
            )
        )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_previous"), callback_data=Admin(section="panels", page=page - 1).pack()
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_next"), callback_data=Admin(section="panels", page=page + 1).pack()
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="home").pack()))
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
    kb.button(text=t("btn_back"), callback_data=Admin(section="fhome"))
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
        kb.button(text=t("btn_back"), callback_data=Admin(section="gset", action="show"))
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
    if group == "growth":
        kb.button(text=t("adm_trial_reset_all"), callback_data=Admin(section="trialreset"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="settings", action="show"))
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


# --- one customer's hidden panels and trials (CAT-10, CAT-11) ---------------------------------


@router.callback_query(Admin.filter(F.section == "uhide"), StaffOnly("customers.write"))
async def hidden_panels(
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
    panels = list(await session.scalars(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id)))
    hidden = [int(p) for p in (customer.hidden_panel_ids or [])]
    if callback_data.action == "toggle":
        if callback_data.page not in {p.id for p in panels}:
            await call.answer(t("button_expired"), show_alert=True)
            return
        before = list(hidden)
        if callback_data.page in hidden:
            hidden.remove(callback_data.page)
        else:
            hidden.append(callback_data.page)
        customer.hidden_panel_ids = sorted(hidden)
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="customer.hidden_panels",
            entity="customer",
            entity_id=customer.id,
            before={"hidden_panel_ids": before},
            after={"hidden_panel_ids": customer.hidden_panel_ids},
        )
        await session.commit()
    await call.answer()
    kb = InlineKeyboardBuilder()
    for panel in panels:
        mark = "🙈" if panel.id in hidden else "👁"
        kb.button(
            text=f"{mark} {panel.name}"[:64],
            callback_data=Admin(section="uhide", action="toggle", id=customer.id, page=panel.id),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id))
    kb.adjust(1)
    text = t("adm_hide_title", name=esc(customer.display_name)) if panels else t("adm_hide_no_panels")
    await show(call, text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "utrial"), StaffOnly("customers.write"))
async def trial_again(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.core import trials

    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    if not await trials.allow_again(session, customer):
        await call.answer(t("adm_trial_not_used"), show_alert=True)
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.trial_again",
        entity="customer",
        entity_id=customer.id,
    )
    await session.commit()
    await call.answer(t("adm_trial_again_done"), show_alert=True)


@router.callback_query(Admin.filter(F.section == "trialreset"), StaffOnly("settings.write"))
async def trial_reset_all(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.core import trials

    if callback_data.action != "go":
        await call.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text=t("adm_trial_reset_yes"), callback_data=Admin(section="trialreset", action="go"))
        kb.button(text=t("btn_back"), callback_data=Admin(section="settings"))
        kb.adjust(1)
        await show(call, t("adm_trial_reset_confirm"), kb.as_markup())
        return
    generation = await trials.new_round(session, shop.id)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.update",
        entity="shop",
        entity_id=shop.id,
        after={"trial_generation": generation},
    )
    await session.commit()
    await call.answer(t("adm_trial_reset_done"), show_alert=True)
    await show(call, t("adm_trial_reset_done"), keyboards.back(t, Admin(section="settings").pack()))


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
        batch = secrets.token_hex(6)
        await state.update_data(gift_audience=audience, gift_batch=batch)
        await show(
            call,
            t("adm_gift_confirm", amount=money(amount), count=count, audience=t(f"adm_aud_{audience}")),
            keyboards.confirm(
                t,
                Admin(section="gift_all", action="go", id=int(batch, 16)).pack(),
                Admin(section="home").pack(),
            ),
        )
        return
    if callback_data.action == "go":
        batch = str(data.get("gift_batch") or "")
        if not batch or callback_data.id != int(batch, 16):
            await call.answer(t("button_expired"), show_alert=True)
            return
        audience = str(data.get("gift_audience") or "")
        if audience not in AUDIENCES:
            await call.answer()
            return
        await state.clear()
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


@router.message(AdminStates.gift_amount, F.text, StaffOnly("wallet.adjust"))
async def gift_amount(message: Message, t: Texts, state: FSMContext) -> None:
    amount = parse_int(message.text)
    if amount is None or not 1_000 <= amount <= 10_000_000:
        await message.answer(t("adm_gift_amount_invalid"))
        return
    await state.set_state(None)
    await state.update_data(gift_amount=amount, gift_batch=secrets.token_hex(6))
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
    if section == "bc":
        # Foxima parity: the buyers of one panel, and those who have not used the bot for a while.
        kb.button(text=t("adm_aud_panel"), callback_data=Admin(section="bc", action="aud_panel", id=row_id))
        kb.button(text=t("adm_aud_idle"), callback_data=Admin(section="bc", action="aud_idle", id=row_id))
    kb.button(text=t("btn_cancel"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


async def _audience_count(
    session: AsyncSession, shop_id: int, audience: str, *, panel_id: int | None = None, idle_days: int = 0
) -> int:
    from paurubot.notifications.handlers import audience_conditions

    conditions = audience_conditions(shop_id, audience, panel_id=panel_id, idle_days=idle_days)
    return int(await session.scalar(select(func.count(Customer.id)).where(*conditions)) or 0)


def _audience_label(t: Texts, audience: dict[str, Any]) -> str:
    kind = audience.get("kind", "all")
    if kind == "idle":
        return str(t("adm_aud_idle_days", days=int(audience.get("idle_days") or 0)))
    return str(t(f"adm_aud_{kind}"))


def action_label(t: Texts, action: str) -> str:
    """A section button's text: the menu button it stands for (Foxima parity)."""
    from paurubot.bot.menu import BUTTONS

    return str(t("adm_bc_act_home") if action == "home" else t(BUTTONS[action]))


# --- broadcast options ---------------------------------------------------------------------


def broadcast_kb(t: Texts, row: Broadcast) -> InlineKeyboardMarkup:
    audience = (row.audience or {}).get("kind", "all")
    pin = (row.audience or {}).get("pin_hours") if (row.audience or {}).get("pin") else None
    forward = bool((row.audience or {}).get("forward"))
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("adm_bc_audience", audience=_audience_label(t, row.audience or {"kind": audience})),
        callback_data=Admin(section="bc", action="aud_menu", id=row.id),
    )
    if pin is None:
        pin_text = t("adm_bc_pin_off")
    elif pin:
        pin_text = t("adm_bc_pin_hours", hours=pin)
    else:
        pin_text = t("adm_bc_pin_on")
    kb.button(text=pin_text, callback_data=Admin(section="bc", action="pin", id=row.id))
    if row.source_message_id:
        kb.button(
            text=t("adm_bc_forward_on") if forward else t("adm_bc_forward_off"),
            callback_data=Admin(section="bc", action="forward", id=row.id),
        )
    kb.button(
        text=t("adm_bc_button_set", text=esc(row.buttons[0]["text"]))
        if row.buttons
        else t("adm_bc_button_add"),
        callback_data=Admin(section="bc", action="button", id=row.id),
    )
    # Foxima parity: a button that opens a section of the bot (buy, trial, wallet...).
    kb.button(text=t("adm_bc_action_add"), callback_data=Admin(section="bc", action="act_menu", id=row.id))
    kb.button(text=t("adm_bc_test"), callback_data=Admin(section="bc", action="test", id=row.id))
    kb.button(text=t("adm_bc_send"), callback_data=Admin(section="bc", action="send", id=row.id))
    kb.button(text=t("btn_cancel"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


async def broadcast_options_text(session: AsyncSession, t: Texts, row: Broadcast) -> str:
    audience = row.audience or {}
    count = await _audience_count(
        session,
        row.shop_id,
        audience.get("kind", "all"),
        panel_id=audience.get("panel_id"),
        idle_days=int(audience.get("idle_days") or 0),
    )
    return t("adm_bc_options", count=count)


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
    elif action == "aud_panel":
        from paurubot.db.models import Panel

        panels = await session.scalars(
            select(Panel).where(Panel.shop_id == shop.id, Panel.is_active.is_(True)).order_by(Panel.id)
        )
        kb = InlineKeyboardBuilder()
        for option in panels:
            kb.button(
                text=option.name[:60],
                callback_data=Admin(section="bc", action="panel", id=row.id, page=option.id),
            )
        kb.button(text=t("btn_back"), callback_data=Admin(section="bc", action="aud_menu", id=row.id))
        kb.adjust(1)
        await call.answer()
        await show(call, t("adm_bc_pick_panel"), kb.as_markup())
        return
    elif action == "panel":
        from paurubot.db.models import Panel

        panel = await session.get(Panel, callback_data.page)
        if panel is None or panel.shop_id != shop.id:
            await call.answer(t("button_expired"), show_alert=True)
            return
        row.audience = {**(row.audience or {}), "kind": "panel", "panel_id": panel.id}
    elif action == "aud_idle":
        await call.answer()
        await state.set_state(AdminStates.broadcast_idle)
        await state.update_data(broadcast_id=row.id)
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_bc_idle_ask"), reply_markup=keyboards.cancel(t))
        return
    elif action == "act_menu":
        from paurubot.bot.routers.start import item_shown
        from paurubot.core.broadcasts import ACTIONS

        cfg = await shop_settings.load(session, shop.id)
        kb = InlineKeyboardBuilder()
        for index, item in enumerate(ACTIONS):
            if item != "home" and not item_shown(item, cfg):
                continue  # switched off or not in the menu: its button would open nothing
            kb.button(
                text=action_label(t, item),
                callback_data=Admin(section="bc", action="act", id=row.id, page=index),
            )
        kb.button(text=t("btn_back"), callback_data=Admin(section="bc", action="view", id=row.id))
        kb.adjust(2)
        await call.answer()
        await show(call, t("adm_bc_pick_action"), kb.as_markup())
        return
    elif action == "act":
        from paurubot.core.broadcasts import ACTIONS

        item = ACTIONS[callback_data.page % len(ACTIONS)]
        row.buttons = [{"text": action_label(t, item), "action": item}]
    elif action == "pin":
        current = (row.audience or {}).get("pin_hours") if (row.audience or {}).get("pin") else None
        following = PIN_CYCLE[(PIN_CYCLE.index(current) + 1) % len(PIN_CYCLE)] if current in PIN_CYCLE else 0
        row.audience = {**(row.audience or {}), "pin": following is not None, "pin_hours": following or 0}
    elif action == "forward":
        row.audience = {**(row.audience or {}), "forward": not (row.audience or {}).get("forward")}
    elif action == "button":
        await call.answer()
        await state.set_state(AdminStates.broadcast_button)
        await state.update_data(broadcast_id=row.id)
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_bc_button_ask"), reply_markup=keyboards.cancel(t))
        return
    elif action == "test":
        # F-16: the staff member sees exactly what customers will get before sending to everyone.
        from paurubot.notifications import handlers, telegram

        try:
            bot = await telegram.bot_for(shop.id)
            if bot is None:
                raise telegram.Undeliverable("bot stopped")
            await telegram.deliver(handlers.broadcast_copier(bot, row)(call.from_user.id))
        except Exception:
            await call.answer(t("adm_bc_test_failed"), show_alert=True)
            return
        await call.answer(t("adm_bc_test_sent"))
        return
    elif action == "send":
        from paurubot.core import broadcasts

        await broadcasts.start(session, row)
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
        await show(call, broadcast_status_text(t, row), broadcast_status_kb(t, row))
        return
    await session.commit()
    await call.answer()
    await show(call, await broadcast_options_text(session, t, row), broadcast_kb(t, row))


# --- a running broadcast: progress, pause, resume, cancel (F-16) ------------------------------


def broadcast_status_text(t: Texts, row: Broadcast) -> str:
    from paurubot.core import broadcasts

    figures = broadcasts.progress(row)
    return t("adm_bc_status", id=row.id, **{**figures, "status": t(f"adm_bc_state_{figures['status']}")})


def broadcast_status_kb(t: Texts, row: Broadcast) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("adm_bc_refresh"), callback_data=Admin(section="bcc", action="view", id=row.id))
    if row.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        kb.button(text=t("adm_bc_pause"), callback_data=Admin(section="bcc", action="pause", id=row.id))
    if row.status == JobStatus.PAUSED:
        kb.button(text=t("adm_bc_resume"), callback_data=Admin(section="bcc", action="resume", id=row.id))
    if row.status not in (JobStatus.DONE, JobStatus.CANCELED):
        kb.button(text=t("adm_bc_cancel"), callback_data=Admin(section="bcc", action="cancel", id=row.id))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == "bcc"), StaffOnly("broadcast.send"))
async def broadcast_control(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.core import broadcasts

    row = await session.get(Broadcast, callback_data.id)
    if row is None or row.shop_id != shop.id:
        await call.answer(t("button_expired"), show_alert=True)
        return
    action = callback_data.action
    steps = {
        "pause": (broadcasts.pause, "adm_bc_paused"),
        "resume": (broadcasts.resume, "adm_bc_resumed"),
        "cancel": (broadcasts.cancel, "adm_bc_canceled"),
    }
    if action in steps:
        step, done_key = steps[action]
        if not await step(session, row):
            await call.answer(t("adm_application_done"), show_alert=True)
        else:
            await record(
                session,
                shop_id=shop.id,
                actor=actor(staff),
                action=f"broadcast.{action}",
                entity="broadcast",
                entity_id=row.id,
            )
            await session.commit()
            await call.answer(t(done_key))
    else:
        await session.refresh(row)
        await call.answer()
    await show(call, broadcast_status_text(t, row), broadcast_status_kb(t, row))


@router.message(AdminStates.broadcast_idle, F.text)
async def broadcast_idle_days(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """How many days without the bot puts a customer in the "idle" audience (Foxima parity)."""
    from paurubot.core.broadcasts import IDLE_DAYS_MAX

    data = await state.get_data()
    row = await session.get(Broadcast, int(data.get("broadcast_id") or 0))
    if row is None or row.shop_id != shop.id or row.status != JobStatus.DRAFT:
        await state.clear()
        return
    raw = (
        (message.text or "")
        .strip()
        .translate(
            str.maketrans("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "0123456789")
        )
    )
    if not raw.isdigit() or not 1 <= int(raw) <= IDLE_DAYS_MAX:
        await message.answer(t("adm_bc_idle_invalid", max=IDLE_DAYS_MAX))
        return
    row.audience = {**(row.audience or {}), "kind": "idle", "idle_days": int(raw)}
    await state.clear()
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(await broadcast_options_text(session, t, row), reply_markup=broadcast_kb(t, row))


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


# --- card-to-card trust and registered cards (PAY-17, PAY-18) --------------------------------


@router.callback_query(Admin.filter(F.section == "cardv"), StaffOnly("payments.review"))
async def card_review(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.core import cards
    from paurubot.db.models import VerifiedCard

    card = await session.get(VerifiedCard, callback_data.id)
    if card is None or card.shop_id != shop.id:
        await call.answer(t("button_expired"), show_alert=True)
        return
    approve = callback_data.action == "ok"
    if not await cards.review(session, card, approve, staff.id):
        await call.answer(t("adm_application_done"), show_alert=True)
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="card.approve" if approve else "card.reject",
        entity="customer",
        entity_id=card.customer_id,
        after={"card": card.masked},
    )
    await queue.enqueue(
        session,
        "notify",
        "card_verify_result",
        {"customer_id": card.customer_id, "approved": approve, "card": card.masked},
        shop_id=shop.id,
        dedupe_key=f"card:{card.id}:result",
    )
    await session.commit()
    await call.answer(t("adm_saved"))
    if isinstance(call.message, Message):
        mark = t("btn_approve") if approve else t("btn_reject")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply(f"{mark} — {esc(staff.staff.display_name if staff.staff else '')}")
        except Exception:  # an old message: the decision is saved either way
            pass


@router.callback_query(Admin.filter(F.section == "utrust"), StaffOnly("payments.review"))
async def card_trust(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Normal → trusted (receipts approved at once) → blocked (no card to card) → normal."""
    from paurubot.core.cards import TRUST_LEVELS

    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    before = customer.card_trust
    index = TRUST_LEVELS.index(before) if before in TRUST_LEVELS else 0
    customer.card_trust = TRUST_LEVELS[(index + 1) % len(TRUST_LEVELS)]
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.card_trust",
        entity="customer",
        entity_id=customer.id,
        before={"card_trust": before},
        after={"card_trust": customer.card_trust},
    )
    await session.commit()
    await call.answer(t(f"adm_trust_{customer.card_trust}"), show_alert=True)
    text, kb = await customer_card(session, t, customer, staff)
    await show(call, text, kb)


@router.callback_query(Admin.filter(F.section == "bc_unpin"), StaffOnly("broadcast.send"))
async def unpin_all(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Take down every broadcast still pinned in customers' chats (GRW-06)."""
    if callback_data.action != "go":
        await call.answer()
        await show(
            call,
            t("adm_bc_unpin_confirm"),
            keyboards.confirm(t, Admin(section="bc_unpin", action="go").pack(), Admin(section="home").pack()),
        )
        return
    await queue.enqueue(
        session, "broadcast", "unpin", {"all": True}, shop_id=shop.id, dedupe_key=f"unpin:{shop.id}:{call.id}"
    )
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="broadcast.unpin_all",
        entity="shop",
        entity_id=shop.id,
    )
    await session.commit()
    await call.answer(t("adm_bc_unpin_queued"), show_alert=True)
    await show(call, t("adm_bc_unpin_queued"), back_home(t))


# --- appearance: premium emoji, start media, buy banner, menu mode (BOT-10..BOT-12) ----------

MENU_MODES = ("reply", "inline", "miniapp")


def _look_text(t: Texts, cfg: dict[str, Any]) -> str:
    from paurubot.bot.premium_emoji import clean_map

    media = cfg.get("start_media") or {}
    return t(
        "adm_look_title",
        emoji_state=t("adm_on") if cfg.get("premium_emoji_enabled") else t("adm_off"),
        emoji_count=len(clean_map(cfg.get("premium_emojis"))),
        start_media=t(f"adm_media_{media['type']}") if media.get("type") else "—",
        banner=t("adm_on") if cfg.get("buy_banner_file_id") else "—",
        menu=t(f"adm_menu_{cfg.get('menu_mode') if cfg.get('menu_mode') in MENU_MODES else 'reply'}"),
    )


def _look_kb(t: Texts, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("adm_look_emoji_off") if cfg.get("premium_emoji_enabled") else t("adm_look_emoji_on"),
        callback_data=Admin(section="look", action="emoji_toggle"),
    )
    kb.button(text=t("adm_look_emoji_add"), callback_data=Admin(section="look", action="emoji_add"))
    kb.button(text=t("adm_look_emoji_clear"), callback_data=Admin(section="look", action="emoji_clear"))
    kb.button(text=t("adm_look_start_set"), callback_data=Admin(section="look", action="start_set"))
    kb.button(text=t("adm_look_start_clear"), callback_data=Admin(section="look", action="start_clear"))
    kb.button(text=t("adm_look_banner_set"), callback_data=Admin(section="look", action="banner_set"))
    kb.button(text=t("adm_look_banner_clear"), callback_data=Admin(section="look", action="banner_clear"))
    kb.button(text=t("adm_look_menu"), callback_data=Admin(section="look", action="menu"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(1, 2, 2, 2, 1, 1)
    return kb.as_markup()


async def _save_look(
    session: AsyncSession, shop: ShopRow, staff: StaffIdentity, values: dict[str, Any]
) -> None:
    from paurubot.bot import premium_emoji

    await shop_settings.set_many(session, shop.id, values)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="settings.appearance",
        entity="shop",
        entity_id=shop.id,
        after={k: (len(v) if k == "premium_emojis" else v) for k, v in values.items()},
    )
    await session.commit()
    premium_emoji.forget(shop.id)


@router.callback_query(Admin.filter(F.section == "look"), StaffOnly("settings.write"))
async def look(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    action = callback_data.action
    asks = {
        "emoji_add": AdminStates.premium_emoji,
        "start_set": AdminStates.start_media,
        "banner_set": AdminStates.buy_banner,
    }
    if action in asks:
        await call.answer()
        await state.set_state(asks[action])
        if isinstance(call.message, Message):
            await call.message.answer(t(f"adm_look_ask_{action}"), reply_markup=keyboards.cancel(t))
        return
    changes: dict[str, Any] = {}
    if action == "emoji_toggle":
        changes["premium_emoji_enabled"] = not cfg.get("premium_emoji_enabled")
    elif action == "emoji_clear":
        changes["premium_emojis"] = {}
    elif action == "start_clear":
        changes["start_media"] = {}
    elif action == "banner_clear":
        changes["buy_banner_file_id"] = ""
    elif action == "menu":
        current = cfg.get("menu_mode") if cfg.get("menu_mode") in MENU_MODES else "reply"
        changes["menu_mode"] = MENU_MODES[(MENU_MODES.index(current) + 1) % len(MENU_MODES)]
    if changes:
        await _save_look(session, shop, staff, changes)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
    await call.answer()
    await show(call, _look_text(t, cfg), _look_kb(t, cfg))


@router.message(AdminStates.premium_emoji)
async def premium_emoji_add(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """Send a message with premium emoji: each one replaces its ordinary emoji from now on."""
    from paurubot.bot.premium_emoji import clean_map

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    found = {
        entity.extract_from(text): str(entity.custom_emoji_id)
        for entity in entities
        if entity.type == "custom_emoji" and entity.custom_emoji_id
    }
    if not found:
        await message.answer(t("adm_look_emoji_none"))
        return
    await state.clear()
    current = clean_map((await shop_settings.load(session, shop.id, fresh=True)).get("premium_emojis"))
    merged = clean_map({**current, **found})
    await _save_look(session, shop, staff, {"premium_emojis": merged, "premium_emoji_enabled": True})
    await message.answer(
        t("adm_look_emoji_saved", count=len(found), total=len(merged)), reply_markup=menu(t, cfg, staff)
    )


@router.message(AdminStates.start_media)
async def start_media_set(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    media: dict[str, str] | None = None
    if message.sticker:
        media = {"type": "sticker", "file_id": message.sticker.file_id}
    elif message.animation:
        media = {"type": "animation", "file_id": message.animation.file_id}
    elif message.photo:
        media = {"type": "photo", "file_id": message.photo[-1].file_id}
    if media is None:
        await message.answer(t("adm_look_media_expected"))
        return
    await state.clear()
    await _save_look(session, shop, staff, {"start_media": media})
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


@router.message(AdminStates.buy_banner, F.photo)
async def buy_banner_set(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    assert message.photo
    await state.clear()
    await _save_look(session, shop, staff, {"buy_banner_file_id": message.photo[-1].file_id})
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))


# --- staff note on a service (SVC-16) ---------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "svcnote"), StaffOnly("services.write"))
async def service_note_ask(call: CallbackQuery, callback_data: Admin, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.service_note)
    await state.update_data(service_id=callback_data.id)
    if isinstance(call.message, Message):
        await call.message.answer(t("adm_service_note_ask"), reply_markup=keyboards.cancel(t))


@router.message(AdminStates.service_note, F.text)
async def service_note_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.clear()
    service = await _service(session, shop, int(data.get("service_id") or 0))
    if service is None:
        await message.answer(t("adm_service_not_found"), reply_markup=menu(t, cfg, staff))
        return
    text = (message.text or "").strip()
    service.staff_note = None if text == "-" else text[:255]
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="service.note",
        entity="service",
        entity_id=service.id,
        after={"staff_note": service.staff_note},
    )
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(service_text(t, service), reply_markup=service_kb(t, service, staff))
