"""Panel admin accounts in the bot (TEN-14): buying one, its login, traffic, extra days, user
slots and renewal, a new password and pausing it."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Acc
from paurubot.bot.states import AccountStates
from paurubot.bot.utils import Btn, error_text, show
from paurubot.core import account_auto_renew, orders, panel_accounts
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import AccountPlan, Customer, OrderKind, PanelAccount
from paurubot.i18n import Texts
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, PanelError
from paurubot.payments import engine as payments

router = Router(name="accounts")
GB_STEPS = [10, 50, 100, 200, 500]
DAY_STEPS = [7, 15, 30, 60, 90]
USER_STEPS = [5, 10, 20, 50, 100]
# The two add-ons sold by amount within the shop's least and most (extra traffic and time).
RANGED = {"gb": OrderKind.ACCOUNT_GB, "days": OrderKind.ACCOUNT_DAYS}
STATUS_ICON = {"active": "🟢", "paused": "⏸", "expired": "⌛️", "deleted": "🗑"}


def steps_within(steps: list[int], low: int, high: int) -> list[int]:
    """The one-tap amounts inside the shop's range; the range's own ends when none fit."""
    inside = [step for step in steps if low <= step <= high]
    return inside or sorted({low, high})


def renewal_line(t: Texts, days: int, gb: int) -> str:
    return t("acc_renew_line", days=days, gb=gb or t("unlimited"))


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


def login_message(t: Texts, account: PanelAccount, password: str) -> tuple[str, InlineKeyboardMarkup]:
    """The panel login details, with a button that copies each value.

    Not sent as protected content: Telegram blocks copying a protected message on phones, so
    the username and password could not be pasted into the panel at all.
    """
    text = t(
        "acc_login",
        url=esc(panel_accounts.login_url(account)),
        username=esc(account.username),
        password=esc(password),
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("acc_btn_copy_username"), copy_text=CopyTextButton(text=account.username)
                )
            ],
            [InlineKeyboardButton(text=t("acc_btn_copy_password"), copy_text=CopyTextButton(text=password))],
        ]
    )
    return text, kb


def account_kb(t: Texts, account: PanelAccount) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    plan = account.plan
    kb.button(text=t("acc_btn_login"), callback_data=Acc(action="login", id=account.id))
    kb.button(text=t("acc_btn_refresh"), callback_data=Acc(action="view", id=account.id, value=1))
    if plan is not None and plan.is_active:
        if plan.renewable:
            kb.button(text=t("acc_btn_renew"), callback_data=Acc(action="renew", id=account.id))
        if plan.gb_price > 0 and account.data_limit and account.status != "expired":
            kb.button(text=t("acc_btn_gb"), callback_data=Acc(action="gb", id=account.id))
        if panel_accounts.can_buy_days(account, plan) and account.status != "expired":
            kb.button(text=t("acc_btn_days"), callback_data=Acc(action="days", id=account.id))
        if plan.user_price > 0 and account.max_users and account.status != "expired":
            kb.button(text=t("acc_btn_users"), callback_data=Acc(action="users", id=account.id))
    if account_auto_renew.choices(account) and account.status != "deleted":
        on = account_auto_renew.is_on(account)
        kb.button(
            text=t("acc_btn_auto_on" if on else "acc_btn_auto_off"),
            callback_data=Acc(action="auto", id=account.id),
        )
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
    items = await panel_accounts.plans(session, customer.shop_id, customer)
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
    plan = await panel_accounts.plan_for(session, customer.shop_id, callback_data.id, customer)
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
    event: CallbackQuery | Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
    kind: OrderKind,
    *,
    plan: AccountPlan | None = None,
    account: PanelAccount | None = None,
    amount: int = 0,
    package: int | None = None,
    renew_days: int = 0,
    renew_gb: int = 0,
) -> None:
    data = await state.get_data()
    if data.get("order_id"):
        await orders.cancel(session, int(data["order_id"]), customer.id)
    try:
        draft = await panel_accounts.draft(
            session,
            customer,
            kind,
            plan=plan,
            account=account,
            amount=amount,
            package=package,
            renew_days=renew_days,
            renew_gb=renew_gb,
        )
        order = await orders.create(session, customer, draft)
    except orders.OrderError as exc:
        if isinstance(event, CallbackQuery):
            await event.answer(error_text(t, exc.key, exc.params), show_alert=True)
        else:
            await event.answer(error_text(t, exc.key, exc.params))
        return
    if isinstance(event, CallbackQuery):
        await event.answer()
    await state.set_state(None)
    await state.set_data({"order_id": order.id})
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    methods = await payments.available(session, customer.shop_id, customer)
    what = {
        OrderKind.ACCOUNT_PURCHASE: lambda: t("acc_checkout_buy", title=esc(plan.title if plan else "")),
        OrderKind.ACCOUNT_RENEW: lambda: t(
            "acc_checkout_renew", username=esc(account.username if account else "")
        ),
        OrderKind.ACCOUNT_GB: lambda: t("acc_checkout_gb", value=amount),
        OrderKind.ACCOUNT_USERS: lambda: t("acc_checkout_users", value=amount),
        OrderKind.ACCOUNT_DAYS: lambda: t("acc_checkout_days", value=amount),
    }[kind]()
    renew = (order.params or {}).get("renew")
    if renew:
        what += "\n" + renewal_line(t, int(renew["days"]), int(renew["gb"]))
    text = t("acc_checkout", what=what, amount=money(order.amount), balance=money(balance))
    markup = keyboards.checkout_kb(
        t,
        order_id=order.id,
        can_wallet=balance >= order.amount,
        methods=methods if balance < order.amount else [],
        discount=False,
    )
    await session.commit()
    await show(event, text, markup)


@router.callback_query(Acc.filter(F.action == "buy"))
async def buy(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    plan = await panel_accounts.plan_for(session, customer.shop_id, callback_data.id, customer)
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
        text, kb = login_message(t, account, panel_accounts.password_of(account))
        await call.message.answer(text, reply_markup=kb)


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
    if account is None:
        return
    plan = account.plan
    if plan is None or panel_accounts.plain_renewal(plan):
        await _checkout(call, session, customer, t, state, OrderKind.ACCOUNT_RENEW, account=account)
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    for index, item in enumerate(panel_accounts.renew_packages(plan)):
        kb.button(
            text=t(
                "acc_renew_package",
                days=item["days"],
                gb=item["gb"] or t("unlimited"),
                price=money(item["price"]),
            ),
            callback_data=Acc(action="rpkg", id=account.id, value=index),
        )
    if panel_accounts.custom_renewal(plan):
        kb.button(text=t("acc_btn_renew_custom"), callback_data=Acc(action="rcustom", id=account.id))
    kb.button(text=t("btn_back"), callback_data=Acc(action="view", id=account.id))
    kb.adjust(1)
    await show(call, t("acc_choose_renew"), kb.as_markup())


@router.callback_query(Acc.filter(F.action == "rpkg"))
async def renew_package(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is not None:
        await _checkout(
            call,
            session,
            customer,
            t,
            state,
            OrderKind.ACCOUNT_RENEW,
            account=account,
            package=callback_data.value,
        )


@router.callback_query(Acc.filter(F.action == "rcustom"))
async def renew_custom(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    """A renewal the buyer sizes: days first, then GB when the plan has a quota."""
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    plan = account.plan
    if plan is None or not panel_accounts.custom_renewal(plan):
        await call.answer(t("renew_option_unavailable"), show_alert=True)
        return
    low, high = (await panel_accounts.extra_bounds(session, customer.shop_id, plan))["days"]
    await call.answer()
    await state.set_state(AccountStates.renew_days)
    await state.update_data(account_id=account.id)
    if isinstance(call.message, Message):
        await call.message.answer(
            t("acc_ask_renew_days", min_value=low, max_value=high, price=money(plan.day_price)),
            reply_markup=keyboards.cancel(t),
        )


async def _renewing(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
    cfg: Any,
    staff: Any,
) -> tuple[PanelAccount, AccountPlan] | None:
    """The account a custom renewal is for, still the buyer's and still on offer."""
    from paurubot.bot.routers.start import menu

    data = await state.get_data()
    account = await panel_accounts.owned(session, customer, int(data.get("account_id") or 0))
    plan = account.plan if account is not None else None
    if account is None or plan is None or not panel_accounts.custom_renewal(plan):
        await state.set_state(None)
        await message.answer(t("renew_option_unavailable"), reply_markup=menu(t, cfg, staff))
        return None
    return account, plan


@router.message(AccountStates.renew_days, F.text)
async def renew_days_typed(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.bot.routers.start import menu

    found = await _renewing(message, session, customer, t, state, cfg, staff)
    if found is None:
        return
    account, plan = found
    bounds = await panel_accounts.extra_bounds(session, customer.shop_id, plan)
    low, high = bounds["days"]
    days = parse_int(message.text)
    if days is None or not low <= days <= high:
        await message.answer(t("acc_amount_range", min_value=low, max_value=high))
        return
    if plan.data_gb > 0:
        await state.update_data(renew_days=days)
        await state.set_state(AccountStates.renew_gb)
        low, high = bounds["gb"]
        await message.answer(t("acc_ask_renew_gb", min_value=low, max_value=high, price=money(plan.gb_price)))
        return
    await state.set_state(None)
    await message.answer(t("acc_amount_noted"), reply_markup=menu(t, cfg, staff))
    await _checkout(
        message, session, customer, t, state, OrderKind.ACCOUNT_RENEW, account=account, renew_days=days
    )


@router.message(AccountStates.renew_gb, F.text)
async def renew_gb_typed(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.bot.routers.start import menu

    found = await _renewing(message, session, customer, t, state, cfg, staff)
    if found is None:
        return
    account, plan = found
    low, high = (await panel_accounts.extra_bounds(session, customer.shop_id, plan))["gb"]
    gb = parse_int(message.text)
    if gb is None or not low <= gb <= high:
        await message.answer(t("acc_amount_range", min_value=low, max_value=high))
        return
    days = int((await state.get_data()).get("renew_days") or 0)
    await state.set_state(None)
    await message.answer(t("acc_amount_noted"), reply_markup=menu(t, cfg, staff))
    await _checkout(
        message,
        session,
        customer,
        t,
        state,
        OrderKind.ACCOUNT_RENEW,
        account=account,
        renew_days=days,
        renew_gb=gb,
    )


@router.callback_query(Acc.filter(F.action.in_({"gb", "days", "users"})))
async def extras(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    plan = account.plan
    unit_name = callback_data.action
    if plan is None:
        unit = 0
    elif unit_name == "gb":
        unit = plan.gb_price if account.data_limit else 0
    elif unit_name == "days":
        unit = plan.day_price if panel_accounts.can_buy_days(account, plan) else 0
    else:
        unit = plan.user_price
    if unit <= 0:
        await call.answer(t("extras_unavailable"), show_alert=True)
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    if unit_name in RANGED:
        low, high = (await panel_accounts.extra_bounds(session, customer.shop_id, plan))[unit_name]
        steps = steps_within(GB_STEPS if unit_name == "gb" else DAY_STEPS, low, high)
        text = t(f"acc_choose_{unit_name}_range", min_value=low, max_value=high)
    else:
        steps = USER_STEPS
        text = t("acc_choose_users")
    for step in steps:
        kb.button(
            text=t("acc_extra_option", value=step, price=money(step * unit)),
            callback_data=Acc(action=f"buy_{unit_name}", id=account.id, value=step),
        )
    if unit_name in RANGED:
        kb.button(
            text=t("acc_btn_custom_amount"), callback_data=Acc(action=f"ask_{unit_name}", id=account.id)
        )
    kb.button(text=t("btn_back"), callback_data=Acc(action="view", id=account.id))
    kb.adjust(1)
    await show(call, text, kb.as_markup())


EXTRA_KINDS = {
    "buy_gb": OrderKind.ACCOUNT_GB,
    "buy_days": OrderKind.ACCOUNT_DAYS,
    "buy_users": OrderKind.ACCOUNT_USERS,
}


@router.callback_query(Acc.filter(F.action.in_(set(EXTRA_KINDS))))
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
    kind = EXTRA_KINDS[callback_data.action]
    await _checkout(call, session, customer, t, state, kind, account=account, amount=callback_data.value)


@router.callback_query(Acc.filter(F.action.in_({"ask_gb", "ask_days"})))
async def ask_amount(
    call: CallbackQuery,
    callback_data: Acc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    """Any amount within the shop's range, typed in."""
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    unit_name = callback_data.action.removeprefix("ask_")
    low, high = (await panel_accounts.extra_bounds(session, customer.shop_id, account.plan))[unit_name]
    await call.answer()
    await state.set_state(AccountStates.amount)
    await state.update_data(account_id=account.id, unit=unit_name)
    if isinstance(call.message, Message):
        await call.message.answer(
            t(f"acc_ask_{unit_name}", min_value=low, max_value=high), reply_markup=keyboards.cancel(t)
        )


@router.message(AccountStates.amount, F.text)
async def typed_amount(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.bot.routers.start import menu

    data = await state.get_data()
    unit_name = str(data.get("unit") or "")
    account = await panel_accounts.owned(session, customer, int(data.get("account_id") or 0))
    if account is None or unit_name not in RANGED:
        await state.set_state(None)
        await message.answer(t("account_not_found"), reply_markup=menu(t, cfg, staff))
        return
    low, high = (await panel_accounts.extra_bounds(session, customer.shop_id, account.plan))[unit_name]
    value = parse_int(message.text)
    if value is None or not low <= value <= high:
        await message.answer(t("acc_amount_range", min_value=low, max_value=high))
        return  # still waiting for a number
    await state.set_state(None)
    await message.answer(t("acc_amount_noted"), reply_markup=menu(t, cfg, staff))  # the menu back
    await _checkout(message, session, customer, t, state, RANGED[unit_name], account=account, amount=value)


@router.callback_query(Acc.filter(F.action == "auto"))
async def auto_renew(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    """Auto-renewal from the wallet: off, or on with the plan's renewal or one of its packages."""
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    if account_auto_renew.is_on(account):
        await account_auto_renew.disable(session, account)
        await session.commit()
        await call.answer(t("acc_auto_disabled"))
        await show(call, account_text(t, account), account_kb(t, account))
        return
    options = account_auto_renew.choices(account)
    if not options:
        await call.answer(t("renew_option_unavailable"), show_alert=True)
        return
    if len(options) == 1:
        await _set_auto(call, session, t, account, options[0])
        return
    await call.answer()
    plan = account.plan
    packages = panel_accounts.renew_packages(plan) if plan is not None else []
    kb = InlineKeyboardBuilder()
    for option in options:
        if "package" in option:
            item = packages[option["package"]]
            text = t(
                "acc_renew_package",
                days=item["days"],
                gb=item["gb"] or t("unlimited"),
                price=money(item["price"]),
            )
        else:
            text = t("acc_auto_plain", price=money(plan.price if plan else 0))
        kb.button(
            text=text,
            callback_data=Acc(action="auto_set", id=account.id, value=option.get("package", -1) + 1),
        )
    kb.button(text=t("btn_back"), callback_data=Acc(action="view", id=account.id))
    kb.adjust(1)
    await show(call, t("acc_auto_choose"), kb.as_markup())


async def _set_auto(
    call: CallbackQuery, session: AsyncSession, t: Texts, account: PanelAccount, choice: dict[str, Any]
) -> None:
    try:
        await account_auto_renew.enable(session, account, choice)
    except account_auto_renew.AutoRenewError as exc:
        await call.answer(t(exc.key), show_alert=True)
        return
    await session.commit()
    await call.answer(t("acc_auto_enabled"), show_alert=True)
    await show(call, account_text(t, account), account_kb(t, account))


@router.callback_query(Acc.filter(F.action == "auto_set"))
async def auto_renew_set(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    account = await _mine(call, session, customer, t, callback_data.id)
    if account is None:
        return
    choice = {"package": callback_data.value - 1} if callback_data.value > 0 else {}
    await _set_auto(call, session, t, account, choice)


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
        text, kb = login_message(t, account, new)
        await call.message.answer(text, reply_markup=kb)


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
