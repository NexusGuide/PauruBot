"""Panel admin accounts in the bot (TEN-14): buying one, its login, traffic, user slots and
renewal, a new password and pausing it."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Acc
from paurubot.bot.utils import Btn, error_text, show
from paurubot.core import orders, panel_accounts
from paurubot.core.formatting import esc, jdate, money
from paurubot.db.models import AccountPlan, Customer, OrderKind, PanelAccount
from paurubot.i18n import Texts
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, PanelError
from paurubot.payments import engine as payments

router = Router(name="accounts")
GB_STEPS = [10, 50, 100, 200, 500]
USER_STEPS = [5, 10, 20, 50, 100]
STATUS_ICON = {"active": "🟢", "paused": "⏸", "expired": "⌛️", "deleted": "🗑"}


def _gb(value: int) -> str:
    return f"{round(value / GB, 1):g}"


def plan_line(t: Texts, plan: AccountPlan) -> str:
    return t(
        "acc_plan_line",
        title=esc(plan.title),
        gb=plan.data_gb or t("unlimited"),
        users=plan.max_users or t("unlimited"),
        days=plan.days or t("unlimited"),
        price=money(plan.price),
    )


def account_text(t: Texts, account: PanelAccount) -> str:
    lines = [
        t(
            "acc_card",
            icon=STATUS_ICON.get(account.status, ""),
            username=esc(account.username),
            status=t(f"acc_status_{account.status}"),
            url=esc(panel_accounts.login_url(account)),
            used=_gb(account.used_bytes),
            limit=_gb(account.data_limit) if account.data_limit else t("unlimited"),
            users=account.users_count,
            max_users=account.max_users or t("unlimited"),
            expires=jdate(account.expire_at) if account.expire_at else t("unlimited"),
        )
    ]
    if account.plan is not None:
        lines.append(t("acc_card_plan", title=esc(account.plan.title)))
    return "\n".join(lines)


def account_kb(t: Texts, account: PanelAccount) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    plan = account.plan
    kb.button(text=t("acc_btn_login"), callback_data=Acc(action="login", id=account.id))
    kb.button(text=t("acc_btn_refresh"), callback_data=Acc(action="view", id=account.id, value=1))
    if plan is not None and plan.is_active:
        kb.button(text=t("acc_btn_renew"), callback_data=Acc(action="renew", id=account.id))
        if plan.gb_price > 0 and account.data_limit and account.status != "expired":
            kb.button(text=t("acc_btn_gb"), callback_data=Acc(action="gb", id=account.id))
        if plan.user_price > 0 and account.max_users and account.status != "expired":
            kb.button(text=t("acc_btn_users"), callback_data=Acc(action="users", id=account.id))
    if account.status in {"active", "paused"}:
        kb.button(text=t("acc_btn_password"), callback_data=Acc(action="password", id=account.id))
        if account.status == "active":
            kb.button(text=t("acc_btn_pause"), callback_data=Acc(action="pause", id=account.id))
        else:
            kb.button(text=t("acc_btn_resume"), callback_data=Acc(action="resume", id=account.id))
    kb.button(text=t("btn_back"), callback_data=Acc(action="home"))
    kb.adjust(2)
    return kb.as_markup()


async def show_home(
    event: Message | CallbackQuery, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    if not await panel_accounts.available(session, customer):
        await show(event, t("accounts_unavailable"))
        return
    mine = await panel_accounts.accounts_of(session, customer)
    kb = InlineKeyboardBuilder()
    for account in mine:
        kb.button(
            text=f"{STATUS_ICON.get(account.status, '')} {account.username}",
            callback_data=Acc(action="view", id=account.id),
        )
    kb.button(text=t("acc_btn_buy"), callback_data=Acc(action="plans"))
    kb.adjust(1)
    await show(event, t("acc_home", count=len(mine)), kb.as_markup())


@router.message(Btn("btn_accounts"))
async def accounts_button(message: Message, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await show_home(message, session, customer, t)


@router.callback_query(Acc.filter(F.action == "home"))
async def home(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    await show_home(call, session, customer, t)


@router.callback_query(Acc.filter(F.action == "plans"))
async def plans(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    if not await panel_accounts.available(session, customer):
        await show(call, t("accounts_unavailable"))
        return
    items = await panel_accounts.plans(session, customer.shop_id)
    kb = InlineKeyboardBuilder()
    for plan in items:
        kb.button(text=f"{plan.title} — {money(plan.price)}", callback_data=Acc(action="plan", id=plan.id))
    kb.button(text=t("btn_back"), callback_data=Acc(action="home"))
    kb.adjust(1)
    await show(call, t("acc_plans") if items else t("acc_no_plans"), kb.as_markup())


@router.callback_query(Acc.filter(F.action == "plan"))
async def plan_view(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    plan = await panel_accounts.plan_for(session, customer.shop_id, callback_data.id)
    if plan is None:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    await call.answer()
    text = plan_line(t, plan)
    if plan.description:
        text += "\n\n" + esc(plan.description)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("acc_btn_buy_plan"), callback_data=Acc(action="buy", id=plan.id))
    kb.button(text=t("btn_back"), callback_data=Acc(action="plans"))
    kb.adjust(1)
    await show(call, text, kb.as_markup())


async def _checkout(
    call: CallbackQuery,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
    kind: OrderKind,
    *,
    plan: AccountPlan | None = None,
    account: PanelAccount | None = None,
    amount: int = 0,
) -> None:
    data = await state.get_data()
    if data.get("order_id"):
        await orders.cancel(session, int(data["order_id"]), customer.id)
    try:
        draft = await panel_accounts.draft(session, customer, kind, plan=plan, account=account, amount=amount)
        order = await orders.create(session, customer, draft)
    except orders.OrderError as exc:
        await call.answer(error_text(t, exc.key, exc.params), show_alert=True)
        return
    await call.answer()
    await state.set_data({"order_id": order.id})
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    methods = await payments.available(session, customer.shop_id)
    what = {
        OrderKind.ACCOUNT_PURCHASE: lambda: t("acc_checkout_buy", title=esc(plan.title if plan else "")),
        OrderKind.ACCOUNT_RENEW: lambda: t(
            "acc_checkout_renew", username=esc(account.username if account else "")
        ),
        OrderKind.ACCOUNT_GB: lambda: t("acc_checkout_gb", value=amount),
        OrderKind.ACCOUNT_USERS: lambda: t("acc_checkout_users", value=amount),
    }[kind]()
    text = t("acc_checkout", what=what, amount=money(order.amount), balance=money(balance))
    markup = keyboards.checkout_kb(
        t,
        order_id=order.id,
        can_wallet=balance >= order.amount,
        methods=methods if balance < order.amount else [],
        discount=False,
    )
    await session.commit()
    await show(call, text, markup)


@router.callback_query(Acc.filter(F.action == "buy"))
async def buy(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    plan = await panel_accounts.plan_for(session, customer.shop_id, callback_data.id)
    if plan is None:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    await _checkout(call, session, customer, t, state, OrderKind.ACCOUNT_PURCHASE, plan=plan)


async def _mine(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, account_id: int
) -> Any:
    account = await panel_accounts.owned(session, customer, account_id)
    if account is None:
        await call.answer(t("account_not_found"), show_alert=True)
    return account


@router.callback_query(Acc.filter(F.action == "view"))
async def view(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    if callback_data.value:  # refresh from the panel
        try:
            await panel_accounts.refresh(session, account)
            await session.commit()
        except (PanelError, panel_accounts.ProvisionError):
            await call.answer(t("acc_panel_down"), show_alert=True)
            return
    await call.answer()
    await show(call, account_text(t, account), account_kb(t, account))


@router.callback_query(Acc.filter(F.action == "login"))
async def login(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(
            t(
                "acc_login",
                url=esc(panel_accounts.login_url(account)),
                username=esc(account.username),
                password=esc(panel_accounts.password_of(account)),
            ),
            protect_content=True,
        )


@router.callback_query(Acc.filter(F.action == "renew"))
async def renew(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is not None:
        await _checkout(call, session, customer, t, state, OrderKind.ACCOUNT_RENEW, account=account)


@router.callback_query(Acc.filter(F.action.in_({"gb", "users"})))
async def extras(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    plan = account.plan
    is_gb = callback_data.action == "gb"
    unit = (plan.gb_price if is_gb else plan.user_price) if plan is not None else 0
    if unit <= 0:
        await call.answer(t("extras_unavailable"), show_alert=True)
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    for step in GB_STEPS if is_gb else USER_STEPS:
        kb.button(
            text=t("acc_extra_option", value=step, price=money(step * unit)),
            callback_data=Acc(action="buy_gb" if is_gb else "buy_users", id=account.id, value=step),
        )
    kb.button(text=t("btn_back"), callback_data=Acc(action="view", id=account.id))
    kb.adjust(1)
    await show(call, t("acc_choose_gb" if is_gb else "acc_choose_users"), kb.as_markup())


@router.callback_query(Acc.filter(F.action.in_({"buy_gb", "buy_users"})))
async def buy_extra(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    kind = OrderKind.ACCOUNT_GB if callback_data.action == "buy_gb" else OrderKind.ACCOUNT_USERS
    await _checkout(call, session, customer, t, state, kind, account=account, amount=callback_data.value)


@router.callback_query(Acc.filter(F.action == "password"))
async def password(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    try:
        new = await panel_accounts.change_password(session, account)
    except (PanelError, panel_accounts.ProvisionError):
        await call.answer(t("acc_panel_down"), show_alert=True)
        return
    await session.commit()
    await call.answer(t("acc_password_changed"))
    if isinstance(call.message, Message):
        await call.message.answer(
            t(
                "acc_login",
                url=esc(panel_accounts.login_url(account)),
                username=esc(account.username),
                password=esc(new),
            ),
            protect_content=True,
        )


@router.callback_query(Acc.filter(F.action.in_({"pause", "resume"})))
async def pause(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    try:
        await panel_accounts.set_paused(session, account, callback_data.action == "pause")
    except panel_accounts.AccountError as exc:
        await call.answer(t(exc.key), show_alert=True)
        return
    except (PanelError, panel_accounts.ProvisionError):
        await call.answer(t("acc_panel_down"), show_alert=True)
        return
    await session.commit()
    await call.answer(t("acc_paused" if account.status == "paused" else "acc_resumed"))
    await show(call, account_text(t, account), account_kb(t, account))
