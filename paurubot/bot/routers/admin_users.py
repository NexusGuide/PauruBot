"""User-management hub, order lookup and confirmed bulk service gifts from Telegram."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_navigation import entries, menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, show
from paurubot.core import service_gifts
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Job, JobStatus, Order, Service
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="admin_users")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())
PAGE = 10
DRAFT_KIND = "admin_service_gift_draft"
DRAFT_TTL = timedelta(minutes=15)


def can_bulk(staff: StaffIdentity) -> bool:
    # Free shop-wide service gifts require both service-write and wallet-adjust authority.
    return staff.can("services.write") and staff.can("wallet.adjust")


def home_kb(t: Texts, staff: StaffIdentity) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    items = {item.key: item for item in entries("users", staff)}
    rows = (
        ("adm_users_balance",),
        ("adm_users_referrals",),
        ("adm_users_cards",),
        ("adm_users_negative",),
        ("adm_users_active",),
        ("adm_users_resellers", "adm_users_all"),
        ("adm_users_order", "adm_users_charge"),
        ("adm_users_lookup", "adm_users_message"),
        ("adm_users_bulk",),
    )
    for keys in rows:
        buttons = []
        for key in keys:
            item = items.get(key)
            if item is not None and (key != "adm_users_bulk" or can_bulk(staff)):
                buttons.append(InlineKeyboardButton(text=t(key), callback_data=item.target().pack()))
        if buttons:
            kb.row(*buttons)
    if staff.can("resellers.manage"):
        kb.row(
            InlineKeyboardButton(
                text=t("adm_users_reseller_ranking"), callback_data=Admin(section="rrank").pack()
            )
        )
    kb.row(
        InlineKeyboardButton(text=t("adm_nav_home"), callback_data=Admin(section="home").pack()),
        InlineKeyboardButton(
            text=t("adm_nav_back"), callback_data=Admin(section="nav", action="exit").pack()
        ),
    )
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == "users"), StaffOnly("customers.read"))
async def users_home(call: CallbackQuery, t: Texts, staff: StaffIdentity, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, t("adm_users_home"), home_kb(t, staff))


@router.callback_query(Admin.filter(F.section == "rrank"), StaffOnly("resellers.manage"))
async def reseller_ranking(call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    from paurubot.core.reseller_ranking import rankings

    rows = await rankings(session, shop.id)
    await call.answer()
    lines = [t("adm_reseller_ranking_title")]
    if not rows:
        lines.append(t("adm_reseller_ranking_empty"))
    for row in rows:
        lines.append(
            t(
                "adm_reseller_ranking_line",
                rank=row.rank,
                name=row.name,
                score=row.score,
                sales=row.sales,
                revenue=row.revenue,
                complaints=row.complaints,
            )
        )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="users").pack()))
    await show(call, "\n\n".join(lines), kb.as_markup())


def order_query(raw: str) -> str | None:
    raw = raw.strip().lstrip("#").strip()
    number = parse_int(raw)
    if number is not None:
        return str(number) if number > 0 else None
    if raw.isdigit():
        return None
    return raw if 1 <= len(raw) <= 64 and not any(c.isspace() for c in raw) else None


async def find_orders(session: AsyncSession, shop_id: int, query: str, page: int) -> list[Order]:
    stmt = select(Order).where(Order.shop_id == shop_id, Order.customer.has(Customer.shop_id == shop_id))
    if query.isdigit():
        stmt = stmt.where(Order.id == int(query))
    else:
        matching = select(Service.id).where(
            Service.shop_id == shop_id, (Service.panel_username == query) | (Service.label == query)
        )
        stmt = stmt.where(Order.service_id.in_(matching))
    return list(await session.scalars(stmt.order_by(Order.id.desc()).offset(page * PAGE).limit(PAGE + 1)))


def orders_kb(t: Texts, orders: list[Order], page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for order in orders[:PAGE]:
        kb.row(
            InlineKeyboardButton(
                text=t(
                    "adm_users_order_button",
                    id=order.id,
                    amount=money(order.amount),
                    status=t(f"adm_users_status_{order.status.value}"),
                ),
                callback_data=Admin(section="uorders", action="view", id=order.id).pack(),
            )
        )
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(
                text=t("btn_prev"),
                callback_data=Admin(section="uorders", action="page", page=page - 1).pack(),
            )
        )
    if len(orders) > PAGE:
        nav.append(
            InlineKeyboardButton(
                text=t("btn_next"),
                callback_data=Admin(section="uorders", action="page", page=page + 1).pack(),
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("adm_users_back"), callback_data=Admin(section="users").pack()))
    return kb.as_markup()


@router.callback_query(Admin.filter(F.section == "uorders"), StaffOnly("orders.read"))
async def order_lookup(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    if not staff.can("customers.read"):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    action = callback_data.action
    if action == "show":
        await state.clear()
        await state.set_state(AdminStates.order_lookup)
        await call.answer()
        if isinstance(call.message, Message):
            await call.message.answer(t("adm_users_order_ask"), reply_markup=keyboards.cancel(t))
        return
    if action == "view":
        order = await session.get(Order, callback_data.id)
        if order is None or order.shop_id != shop.id or order.customer.shop_id != shop.id:
            await call.answer(t("adm_users_not_found"), show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text=t("adm_users_customer_card"),
                callback_data=Admin(section="user", id=order.customer_id).pack(),
            )
        )
        if order.service_id and staff.can("services.read"):
            kb.row(
                InlineKeyboardButton(
                    text=t("adm_users_service_card"),
                    callback_data=Admin(section="svc", id=order.service_id).pack(),
                )
            )
        kb.row(InlineKeyboardButton(text=t("adm_users_back"), callback_data=Admin(section="users").pack()))
        await call.answer()
        await show(
            call,
            t(
                "adm_users_order_text",
                id=order.id,
                customer=esc(order.customer.display_name),
                telegram_id=order.customer.telegram_id,
                kind=t(f"adm_users_kind_{order.kind.value}"),
                status=t(f"adm_users_status_{order.status.value}"),
                amount=money(order.amount),
                created=jdate(order.created_at, with_time=True),
                completed=jdate(order.completed_at, with_time=True),
                service=order.service_id or "—",
            ),
            kb.as_markup(),
        )
        return
    data = await state.get_data()
    query = str(data.get("order_query") or "")
    if action != "page" or not query or not 0 <= callback_data.page <= 1000:
        await call.answer(t("button_expired"), show_alert=True)
        return
    rows = await find_orders(session, shop.id, query, callback_data.page)
    await call.answer()
    await show(
        call,
        t("adm_users_order_results") if rows else t("adm_users_not_found"),
        orders_kb(t, rows, callback_data.page),
    )


@router.message(AdminStates.order_lookup, F.text, StaffOnly("orders.read"))
async def order_input(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not staff.can("customers.read"):
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    query = order_query(message.text or "")
    if query is None:
        await message.answer(t("adm_users_order_invalid"))
        return
    await state.set_state(None)
    await state.update_data(order_query=query)
    rows = await find_orders(session, shop.id, query, 0)
    await message.answer(t("adm_users_order_results"), reply_markup=menu(t, cfg, staff))
    await message.answer(
        t("adm_users_order_results") if rows else t("adm_users_not_found"), reply_markup=orders_kb(t, rows, 0)
    )


def draft_kb(t: Texts, draft_id: int) -> InlineKeyboardMarkup:
    return keyboards.confirm(
        t,
        Admin(section="ubulk", action="confirm", id=draft_id).pack(),
        Admin(section="ubulk", action="cancel", id=draft_id).pack(),
    )


@router.callback_query(Admin.filter(F.section == "ubulk"))
async def bulk_services(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    if not can_bulk(staff):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    action = callback_data.action
    if action == "show":
        await state.clear()
        kb = InlineKeyboardBuilder()
        for mode in ("days", "gb"):
            kb.row(
                InlineKeyboardButton(
                    text=t(f"adm_users_bulk_{mode}"), callback_data=Admin(section="ubulk", action=mode).pack()
                )
            )
        kb.row(InlineKeyboardButton(text=t("adm_users_back"), callback_data=Admin(section="users").pack()))
        await call.answer()
        await show(call, t("adm_users_bulk_intro"), kb.as_markup())
        return
    if action in {"days", "gb"}:
        await state.clear()
        await state.set_state(AdminStates.bulk_service_value)
        await state.update_data(bulk_mode=action)
        await call.answer()
        if isinstance(call.message, Message):
            await call.message.answer(t(f"adm_users_bulk_ask_{action}"), reply_markup=keyboards.cancel(t))
        return
    if action not in {"confirm", "cancel", "status"}:
        await call.answer(t("button_expired"), show_alert=True)
        return
    draft = await session.scalar(select(Job).where(Job.id == callback_data.id).with_for_update())
    if (
        draft is None
        or draft.shop_id != shop.id
        or draft.kind != DRAFT_KIND
        or draft.payload.get("telegram_id") != call.from_user.id
    ):
        await call.answer(t("button_expired"), show_alert=True)
        return
    if action == "status":
        if draft.status != JobStatus.DONE:
            await call.answer(t("button_expired"), show_alert=True)
            return
        children = list(
            await session.scalars(
                select(Job).where(Job.shop_id == shop.id, Job.dedupe_key.like(f"bulk:bot:{draft.id}:%"))
            )
        )
        applied = sum(
            j.status == JobStatus.DONE and j.payload.get("outcome") in {"applied", "already_applied"}
            for j in children
        )
        skipped = sum(
            j.status == JobStatus.DONE and str(j.payload.get("outcome", "")).startswith("skipped")
            for j in children
        )
        failed = sum(j.status in {JobStatus.DEAD, JobStatus.FAILED} for j in children)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text=t("btn_refresh"), callback_data=callback_data.pack()))
        kb.row(InlineKeyboardButton(text=t("adm_users_back"), callback_data=Admin(section="users").pack()))
        await call.answer()
        await show(
            call,
            t(
                "adm_users_bulk_status",
                total=len(children),
                applied=applied,
                skipped=skipped,
                failed=failed,
                pending=len(children) - applied - skipped - failed,
            ),
            kb.as_markup(),
        )
        return
    if draft.status != JobStatus.DRAFT:
        await call.answer(t("adm_users_bulk_already"), show_alert=True)
        return
    if action == "cancel" or utcnow() - draft.created_at > DRAFT_TTL:
        draft.status = JobStatus.CANCELED
        await session.commit()
        await state.clear()
        await call.answer(t("canceled") if action == "cancel" else t("button_expired"))
        await show(call, t("adm_users_home"), home_kb(t, staff))
        return
    targets = draft.payload.get("targets") or []
    queued = 0
    for target in targets:
        job_id = await queue.enqueue(
            session,
            "bulk",
            "service",
            {
                "service_id": target["service_id"],
                "action": "extend",
                "gift_target": target,
                "staff_id": staff.id,
                "batch": f"bot:{draft.id}",
            },
            shop_id=shop.id,
            dedupe_key=f"bulk:bot:{draft.id}:{target['service_id']}",
            max_attempts=5,
        )
        queued += job_id is not None
    draft.status, draft.finished_at = JobStatus.DONE, utcnow()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="service.bulk_extend",
        entity="service",
        after={
            "source": "bot",
            "count": queued,
            "draft_id": draft.id,
            "mode": draft.payload["mode"],
            "value": draft.payload["value"],
        },
    )
    await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=t("adm_users_bulk_progress"),
            callback_data=Admin(section="ubulk", action="status", id=draft.id).pack(),
        )
    )
    kb.row(InlineKeyboardButton(text=t("adm_users_back"), callback_data=Admin(section="users").pack()))
    await call.answer(t("adm_saved"))
    await show(call, t("adm_users_bulk_queued", count=queued), kb.as_markup())


@router.message(AdminStates.bulk_service_value, F.text)
async def bulk_value(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not can_bulk(staff):
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    data = await state.get_data()
    mode, value = str(data.get("bulk_mode") or ""), parse_int(message.text)
    if mode not in {"days", "gb"} or value is None or not 1 <= value <= (3650 if mode == "days" else 100_000):
        await message.answer(t("adm_users_bulk_invalid"))
        return
    try:
        targets = await service_gifts.preview(session, shop.id, mode, value)
    except ValueError:
        await message.answer(t("adm_users_bulk_too_many", limit=service_gifts.LIMIT))
        return
    await state.clear()
    await message.answer(t("adm_users_bulk_preview_title"), reply_markup=menu(t, cfg, staff))
    if not targets:
        await message.answer(t("adm_users_bulk_empty"), reply_markup=home_kb(t, staff))
        return
    draft = Job(
        shop_id=shop.id,
        queue="bulk",
        kind=DRAFT_KIND,
        status=JobStatus.DRAFT,
        payload={
            "telegram_id": message.from_user.id if message.from_user else message.chat.id,
            "mode": mode,
            "value": value,
            "targets": targets,
        },
    )
    session.add(draft)
    await session.commit()
    await message.answer(
        t("adm_users_bulk_confirm", count=len(targets), value=value, unit=t(f"adm_users_bulk_unit_{mode}")),
        reply_markup=draft_kb(t, draft.id),
    )
