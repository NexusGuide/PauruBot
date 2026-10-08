"""The reseller's own panel in the bot (TEN-13): figures, quick sale, finding a service it sold,
services about to run out with one-tap renewal for all, a sales report and its level-2 resellers."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Rs, Shop, Svc, Wallet
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ResellerStates
from paurubot.bot.utils import show
from paurubot.core import reseller_desk, resellers
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import Customer, ResellerProfile, Service
from paurubot.i18n import Texts
from paurubot.ledger.accounts import wallet_balance
from paurubot.worker import queue

router = Router(name="reseller")
STATUS_ICON = keyboards.STATUS_ICON


def current(customer: Customer) -> ResellerProfile | None:
    profile = customer.reseller_profile
    return profile if profile is not None and profile.is_current else None


async def hub_text(session: AsyncSession, customer: Customer, profile: ResellerProfile, t: Texts) -> str:
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    group = profile.group
    lines = [
        t(
            "reseller_status",
            group=esc(group.title if group else "-"),
            percent=profile.discount_percent,
            min_topup=money(group.min_topup if group else 0),
            balance=money(balance),
            expires=jdate(profile.expires_at) if profile.expires_at else t("unlimited"),
        )
    ]
    if profile.billing_mode == "usage":
        lines.append(t("reseller_billing_usage", price=money(profile.gb_price)))
    elif profile.billing_mode == "hourly":
        lines.append(t("reseller_billing_hourly", price=money(profile.hourly_price)))
    if profile.billing_debt:
        lines.append(t("reseller_debt_line", debt=money(profile.billing_debt)))
    if profile.suspended_at is not None:
        lines.append(t("reseller_suspended_line"))
    figures = await reseller_desk.figures(session, customer)
    lines.append(
        t(
            "rs_figures",
            active=figures.active,
            expiring=figures.expiring,
            orders=figures.month_orders,
            amount=money(figures.month_amount),
        )
    )
    return "\n".join(lines)


def hub_kb(t: Texts, profile: ResellerProfile) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("rs_btn_sell"), callback_data=Shop(step="start"))
    kb.button(text=t("rs_btn_search"), callback_data=Rs(action="search"))
    kb.button(text=t("rs_btn_expiring"), callback_data=Rs(action="expiring", value=3))
    kb.button(text=t("rs_btn_report"), callback_data=Rs(action="report"))
    kb.button(text=t("rs_btn_wallet"), callback_data=Wallet(action="home"))
    kb.button(text=t("rs_btn_services"), callback_data=Svc(action="list"))
    if profile.parent_id is None and "resellers.sub" in (profile.permissions or []):
        kb.button(text=t("rs_btn_subs"), callback_data=Rs(action="subs"))
    kb.adjust(2)
    return kb.as_markup()


def back_kb(t: Texts) -> InlineKeyboardMarkup:
    return keyboards.back(t, Rs(action="home").pack())


async def show_hub(
    event: Message | CallbackQuery, session: AsyncSession, customer: Customer, t: Texts
) -> bool:
    profile = current(customer)
    if profile is None:
        return False
    await show(event, await hub_text(session, customer, profile, t), hub_kb(t, profile))
    return True


@router.callback_query(Rs.filter(F.action == "home"))
async def home(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await state.clear()
    await call.answer()
    if not await show_hub(call, session, customer, t):
        await show(call, t("rs_not_reseller"))


def service_line(t: Texts, service: Service) -> str:
    days, left = reseller_desk.remaining_text(service)
    parts = [service.label or service.panel_username]
    if days is not None:
        parts.append(t("rs_days_left", days=days))
    if left is not None:
        parts.append(t("rs_gb_left", gb=left))
    return f"{STATUS_ICON.get(service.status, '')} " + " · ".join(parts)


def services_list_kb(t: Texts, items: list[Service], extra: list[list[InlineKeyboardButton]]) -> Any:
    kb = InlineKeyboardBuilder()
    for service in items:
        kb.row(
            InlineKeyboardButton(
                text=service_line(t, service)[:64], callback_data=Svc(action="view", id=service.id).pack()
            )
        )
    for row in extra:
        kb.row(*row)
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Rs(action="home").pack()))
    return kb.as_markup()


# --- search ------------------------------------------------------------------------------


@router.callback_query(Rs.filter(F.action == "search"))
async def search_ask(call: CallbackQuery, customer: Customer, t: Texts, state: FSMContext) -> None:
    await call.answer()
    if current(customer) is None:
        return
    await state.set_state(ResellerStates.search)
    if isinstance(call.message, Message):
        await call.message.answer(t("rs_search_ask"), reply_markup=keyboards.cancel(t))


@router.message(ResellerStates.search, F.text)
async def search(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    await state.clear()
    found = await reseller_desk.search(session, customer, message.text or "")
    await message.answer(t("rs_search_done", count=len(found)), reply_markup=menu(t, cfg, staff))
    if not found:
        await message.answer(t("rs_search_none"), reply_markup=back_kb(t))
        return
    again = [InlineKeyboardButton(text=t("rs_btn_search_again"), callback_data=Rs(action="search").pack())]
    await message.answer(t("rs_search_results"), reply_markup=services_list_kb(t, found, [again]))


# --- expiring and bulk renewal -----------------------------------------------------------


@router.callback_query(Rs.filter(F.action == "expiring"))
async def expiring(
    call: CallbackQuery, callback_data: Rs, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    if current(customer) is None:
        return
    days = callback_data.value if callback_data.value in (3, 7, 30) else 3
    items = await reseller_desk.expiring(session, customer, days)
    ranges = [
        InlineKeyboardButton(
            text=("• " if d == days else "") + t("rs_within_days", days=d),
            callback_data=Rs(action="expiring", value=d).pack(),
        )
        for d in (3, 7, 30)
    ]
    extra = [ranges]
    if items:
        extra.append(
            [
                InlineKeyboardButton(
                    text=t("rs_btn_renew_all"), callback_data=Rs(action="renew_all", value=days).pack()
                )
            ]
        )
    text = t("rs_expiring", count=len(items), days=days) if items else t("rs_expiring_none", days=days)
    await show(call, text, services_list_kb(t, items, extra))


@router.callback_query(Rs.filter(F.action == "renew_all"))
async def renew_all_ask(
    call: CallbackQuery, callback_data: Rs, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    if current(customer) is None:
        return
    days = callback_data.value if callback_data.value in (3, 7, 30) else 3
    planned = await reseller_desk.renew_plan(
        session, customer, await reseller_desk.expiring(session, customer, days)
    )
    if not planned.items:
        await show(call, t("rs_renew_nothing"), back_kb(t))
        return
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    text = t(
        "rs_renew_confirm",
        count=len(planned.items),
        skipped=len(planned.skipped),
        total=money(planned.total),
        balance=money(balance),
    )
    if balance < planned.total:
        text += "\n" + t("checkout_need_more", amount=money(planned.total - balance))
    await show(
        call,
        text,
        keyboards.confirm(
            t, Rs(action="renew_all_go", value=days).pack(), Rs(action="expiring", value=days).pack()
        ),
    )


@router.callback_query(Rs.filter(F.action == "renew_all_go"))
async def renew_all(
    call: CallbackQuery, callback_data: Rs, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    if current(customer) is None:
        await call.answer()
        return
    await call.answer(t("processing"))
    days = callback_data.value if callback_data.value in (3, 7, 30) else 3
    result = await reseller_desk.renew_many(
        session, customer, await reseller_desk.expiring(session, customer, days)
    )
    await session.commit()
    lines = [t("rs_renew_done", renewed=len(result.renewed), failed=len(result.failed))]
    for service, key in result.failed[:15]:
        reason = t("rs_fail_balance") if key == "insufficient_balance" else t(key)
        lines.append(f"• {esc(service.label or service.panel_username)}: {reason}")
    await show(call, "\n".join(lines), back_kb(t))


# --- sales report ------------------------------------------------------------------------


@router.callback_query(Rs.filter(F.action == "report"))
async def report(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    if current(customer) is None:
        return
    data = await reseller_desk.report(session, customer)
    lines = [t("rs_report_title")]
    for key, count, amount in data.periods:
        lines.append(t(f"rs_report_{key}", orders=count, amount=money(amount)))
    lines.append(t("rs_report_gb", gb=money(data.gb_sold)))
    if data.commission:
        lines.append(t("rs_report_commission", amount=money(data.commission)))
    if data.top_plans:
        lines.append("\n" + t("rs_report_top"))
        for title, count, amount in data.top_plans:
            lines.append(f"• {esc(title)}: {count} — {money(amount)}")
    await show(call, "\n".join(lines), back_kb(t))


# --- level-2 resellers (TEN-01) ----------------------------------------------------------


def _manages_subs(customer: Customer) -> ResellerProfile | None:
    profile = current(customer)
    if profile is None or profile.parent_id is not None or "resellers.sub" not in (profile.permissions or []):
        return None
    return profile


@router.callback_query(Rs.filter(F.action == "subs"))
async def subs(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    profile = _manages_subs(customer)
    if profile is None:
        return
    rows = await resellers.children(session, profile)
    names = {
        c.id: c
        for c in await session.scalars(select(Customer).where(Customer.id.in_([r.customer_id for r in rows])))
    }
    kb = InlineKeyboardBuilder()
    for row in rows:
        who = names.get(row.customer_id)
        state = "✅" if row.is_active else "⏸"
        kb.row(
            InlineKeyboardButton(
                text=f"{state} {who.display_name if who else row.customer_id} · {row.discount_percent}%",
                callback_data=Rs(action="sub", id=row.id).pack(),
            )
        )
    limit = profile.max_subresellers
    if limit is None or len(rows) < limit:
        kb.row(InlineKeyboardButton(text=t("rs_btn_sub_add"), callback_data=Rs(action="sub_add").pack()))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=Rs(action="home").pack()))
    text = t("rs_subs", count=len(rows), max=profile.discount_percent)
    await show(call, text, kb.as_markup())


async def _own_child(
    session: AsyncSession, parent: ResellerProfile, profile_id: int
) -> ResellerProfile | None:
    child = await session.get(ResellerProfile, profile_id)
    return child if child is not None and child.parent_id == parent.id else None


@router.callback_query(Rs.filter(F.action.in_({"sub", "sub_toggle"})))
async def sub(
    call: CallbackQuery, callback_data: Rs, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    parent = _manages_subs(customer)
    child = await _own_child(session, parent, callback_data.id) if parent else None
    if parent is None or child is None:
        await call.answer(t("rs_not_found"), show_alert=True)
        return
    if callback_data.action == "sub_toggle":
        child.is_active = not child.is_active
        await session.flush()
    await call.answer()
    who = await session.get(Customer, child.customer_id)
    balance = await wallet_balance(session, child.shop_id, child.customer_id)
    text = t(
        "rs_sub_card",
        name=esc(who.display_name if who else "-"),
        telegram_id=who.telegram_id if who else "-",
        percent=child.discount_percent,
        balance=money(balance),
        state=t("rs_active") if child.is_active else t("rs_paused"),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=t("rs_btn_sub_discount"), callback_data=Rs(action="sub_discount", id=child.id))
    kb.button(
        text=t("rs_btn_sub_pause") if child.is_active else t("rs_btn_sub_resume"),
        callback_data=Rs(action="sub_toggle", id=child.id),
    )
    kb.button(text=t("btn_back"), callback_data=Rs(action="subs"))
    kb.adjust(2, 1)
    await show(call, text, kb.as_markup())


@router.callback_query(Rs.filter(F.action == "sub_add"))
async def sub_add(call: CallbackQuery, customer: Customer, t: Texts, state: FSMContext) -> None:
    await call.answer()
    if _manages_subs(customer) is None:
        return
    await state.set_state(ResellerStates.sub_telegram_id)
    if isinstance(call.message, Message):
        await call.message.answer(t("rs_sub_ask_id"), reply_markup=keyboards.cancel(t))


@router.message(ResellerStates.sub_telegram_id, F.text)
async def sub_add_id(message: Message, customer: Customer, t: Texts, state: FSMContext) -> None:
    telegram_id = parse_int(message.text)
    if telegram_id is None or telegram_id <= 0:
        await message.answer(t("number_expected"))
        return
    parent = _manages_subs(customer)
    if parent is None:
        await state.clear()
        return
    await state.update_data(sub_telegram_id=telegram_id, sub_profile_id=0)
    await state.set_state(ResellerStates.sub_discount)
    await message.answer(t("rs_sub_ask_discount", max=parent.discount_percent))


@router.callback_query(Rs.filter(F.action == "sub_discount"))
async def sub_discount_ask(
    call: CallbackQuery,
    callback_data: Rs,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    parent = _manages_subs(customer)
    child = await _own_child(session, parent, callback_data.id) if parent else None
    await call.answer()
    if parent is None or child is None:
        return
    await state.set_state(ResellerStates.sub_discount)
    await state.update_data(sub_telegram_id=0, sub_profile_id=child.id)
    if isinstance(call.message, Message):
        await call.message.answer(
            t("rs_sub_ask_discount", max=parent.discount_percent), reply_markup=keyboards.cancel(t)
        )


@router.message(ResellerStates.sub_discount, F.text)
async def sub_discount(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    parent = _manages_subs(customer)
    discount = parse_int(message.text)
    if parent is None:
        await state.clear()
        return
    if discount is None:
        await message.answer(t("number_expected"))
        return
    data = await state.get_data()
    await state.clear()
    try:
        if data.get("sub_profile_id"):
            child = await _own_child(session, parent, int(data["sub_profile_id"]))
            if child is None:
                raise resellers.ResellerError("rs_not_found")
            resellers.check_child_discount(parent, discount)
            child.discount_override = discount
        else:
            target = await session.scalar(
                select(Customer).where(
                    Customer.shop_id == customer.shop_id,
                    Customer.telegram_id == int(data.get("sub_telegram_id") or 0),
                )
            )
            if target is None:
                raise resellers.ResellerError("customer_must_start_bot")
            child = await resellers.make_child(session, parent, target, discount)
            await queue.enqueue(
                session,
                "notify",
                "reseller_decision",
                {"customer_id": target.id, "approved": True},
                shop_id=customer.shop_id,
            )
    except resellers.ResellerError as exc:
        await message.answer(t(exc.key, **exc.params), reply_markup=menu(t, cfg, staff))
        return
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(t("rs_sub_saved", percent=child.discount_percent), reply_markup=back_kb(t))
