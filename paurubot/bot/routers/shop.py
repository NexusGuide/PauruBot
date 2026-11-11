"""Buying: location → category → plan → checkout, for purchases, renewals, extras and upgrades."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Pay, Shop, Svc
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ShopStates
from paurubot.bot.utils import Btn, error_text, show
from paurubot.core import catalog, orders, pricing
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money, parse_int, processing_key
from paurubot.db.models import Customer, DiscountCode, Order, OrderKind, OrderStatus, Plan, Service
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine as payments

router = Router(name="shop")


async def priced(session: AsyncSession, plans: list[Plan], customer: Customer) -> list[tuple[Plan, int]]:
    from paurubot.core import resale

    out = []
    for plan in plans:
        listed = await resale.list_price(session, plan, customer.shop_id)
        out.append((plan, (await pricing.plan_quote(session, customer, plan.id, listed)).amount))
    return out


async def _ask_phone(event: Message | CallbackQuery, t: Texts) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(message, Message):
        await message.answer(t("phone_required_buy"), reply_markup=keyboards.share_phone(t))


async def show_start(
    event: Message | CallbackQuery, session: AsyncSession, shop: ShopRow, customer: Customer, t: Texts
) -> None:
    locations = await catalog.locations(session, shop.id, customer)
    if len(locations) > 1:
        await show(event, t("choose_location"), keyboards.locations_kb(t, locations))
        return
    await show_categories(event, session, shop, customer, t, locations[0].id if locations else 0)


async def show_categories(
    event: Message | CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
    loc: int,
) -> None:
    custom = await custom_available(session, shop.id)
    cats = await catalog.categories(session, shop.id, customer, loc or None)
    loose = await catalog.plans(session, shop.id, customer, location_id=loc or None, uncategorised=True)
    if not cats:
        plans = await catalog.plans(session, shop.id, customer, location_id=loc or None)
        if not plans and not custom:
            await show(event, t("no_plans"))
            return
        await plans_or_durations(
            event,
            session,
            shop,
            customer,
            t,
            plans,
            back_cb=Shop(step="start").pack(),
            loc=loc,
            cat=0,
            every=True,
            custom=custom,
        )
        return
    await show(event, t("choose_category"), keyboards.categories_kb(t, cats, loc, bool(loose), custom=custom))


async def plans_or_durations(
    event: Message | CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
    plans: list[Plan],
    *,
    back_cb: str,
    loc: int,
    cat: int,
    every: bool,
    custom: bool = False,
) -> None:
    """The plans, or first their durations when the shop sells by duration (Foxima parity)."""
    days = sorted({int(p.days) for p in plans})
    if len(days) > 1 and (await shop_settings.load(session, shop.id)).get("buy_by_duration"):
        await show(
            event,
            t("choose_duration"),
            keyboards.durations_kb(t, days, loc=loc, cat=cat, every=every, back_cb=back_cb, custom=custom),
        )
        return
    await show(
        event,
        t("choose_plan"),
        keyboards.plans_kb(t, await priced(session, plans, customer), back_cb=back_cb, custom=custom),
    )


@router.callback_query(Shop.filter(F.step == "dur"))
async def pick_duration(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
) -> None:
    """The plans of one duration (Foxima parity); `kind="all"` is the shop without categories."""
    await call.answer()
    every = callback_data.kind == "all"
    plans = await catalog.plans(
        session,
        shop.id,
        customer,
        location_id=callback_data.loc or None,
        category_id=None if every else (callback_data.cat or None),
        uncategorised=False if every else not callback_data.cat,
    )
    plans = [p for p in plans if int(p.days) == callback_data.n]
    if not plans:
        await show(call, t("no_plans"))
        return
    back = (
        Shop(step="loc", loc=callback_data.loc).pack()
        if every
        else Shop(step="cat", loc=callback_data.loc, cat=callback_data.cat).pack()
    )
    await show(
        call, t("choose_plan"), keyboards.plans_kb(t, await priced(session, plans, customer), back_cb=back)
    )


async def custom_available(session: AsyncSession, shop_id: int) -> bool:
    cfg = await shop_settings.load(session, shop_id)
    return bool(cfg["custom_buy_enabled"] and (cfg["custom_panel_id"] or cfg["custom_location_id"]))


@router.message(Btn("btn_buy"))
async def buy(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    if cfg.get("buy_banner_file_id"):
        try:
            await message.answer_photo(str(cfg["buy_banner_file_id"]))  # BOT-11
        except Exception:  # an expired file id must never block buying
            pass
    await show_start(message, session, shop, customer, t)


@router.callback_query(Shop.filter(F.step == "start"))
async def back_start(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, customer: Customer, t: Texts
) -> None:
    await call.answer()
    await show_start(call, session, shop, customer, t)


@router.callback_query(Shop.filter(F.step == "loc"))
async def pick_location(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
) -> None:
    await call.answer()
    await show_categories(call, session, shop, customer, t, callback_data.loc)


@router.callback_query(Shop.filter(F.step == "cat"))
async def pick_category(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
) -> None:
    await call.answer()
    plans = await catalog.plans(
        session,
        shop.id,
        customer,
        location_id=callback_data.loc or None,
        category_id=callback_data.cat or None,
        uncategorised=not callback_data.cat,
    )
    if not plans:
        await show(call, t("no_plans"))
        return
    back = Shop(step="loc", loc=callback_data.loc).pack()
    await plans_or_durations(
        call,
        session,
        shop,
        customer,
        t,
        plans,
        back_cb=back,
        loc=callback_data.loc,
        cat=callback_data.cat,
        every=False,
    )


@router.callback_query(Shop.filter(F.step == "plan"))
async def pick_plan(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    shop: ShopRow,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    plan = await catalog.get_plan(session, shop.id, callback_data.plan, customer)
    if plan is None:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    kind = (
        OrderKind(callback_data.kind)
        if callback_data.kind
        else (OrderKind.RENEW if callback_data.svc else OrderKind.PURCHASE)
    )
    await call.answer()
    await state.set_data(
        {"kind": kind.value, "plan_id": plan.id, "svc_id": callback_data.svc, "code_id": 0, "label": None}
    )
    if kind == OrderKind.PURCHASE and customer.is_reseller:
        await state.set_state(ShopStates.label)
        if isinstance(call.message, Message):
            await call.message.answer(t("ask_label"), reply_markup=keyboards.cancel(t))
        return
    if kind == OrderKind.PURCHASE and await ask_username(call, t, cfg, state):
        return
    await checkout(call, session, customer, t, cfg, state)


async def ask_username(
    event: Message | CallbackQuery, t: Texts, cfg: dict[str, Any], state: FSMContext
) -> bool:
    """With `custom_username_enabled`, a new service's name comes from the customer (CAT-12)."""
    if not cfg.get("custom_username_enabled"):
        return False
    await state.set_state(ShopStates.username)
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(message, Message):
        await message.answer(t("ask_username"), reply_markup=keyboards.cancel(t))
    return True


@router.message(ShopStates.username, F.text)
async def got_username(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.core import usernames

    text = (message.text or "").strip()
    name = None if text in {"-", "0"} else usernames.wanted(text)
    if text not in {"-", "0"} and name is None:
        await message.answer(t("username_invalid"))
        return
    await state.update_data(wanted_username=name)
    await state.set_state(None)
    await message.answer(
        t("username_saved", name=name) if name else t("username_skipped"), reply_markup=menu(t, cfg, staff)
    )
    await checkout(message, session, customer, t, cfg, state)


@router.callback_query(Shop.filter(F.step == "custom"))
async def custom_start(
    call: CallbackQuery,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    if not await custom_available(session, shop.id):
        await call.answer(t("custom_unavailable"), show_alert=True)
        return
    await call.answer()
    limits = orders.custom_limits(cfg)
    await state.set_data({"kind": OrderKind.PURCHASE.value, "plan_id": 0, "code_id": 0, "label": None})
    await state.set_state(ShopStates.custom_gb)
    if isinstance(call.message, Message):
        await call.message.answer(
            t(
                "ask_custom_gb",
                min=limits["min_gb"],
                max=limits["max_gb"],
                price=money(int(cfg["custom_gb_price"])),
            ),
            reply_markup=keyboards.cancel(t),
        )


@router.message(ShopStates.custom_gb, F.text)
async def custom_gb(message: Message, t: Texts, cfg: dict[str, Any], state: FSMContext) -> None:
    limits = orders.custom_limits(cfg)
    value = parse_int(message.text)
    if value is None or not limits["min_gb"] <= value <= limits["max_gb"]:
        await message.answer(t("custom_gb_invalid", min=limits["min_gb"], max=limits["max_gb"]))
        return
    await state.update_data(custom_gb=value)
    await state.set_state(ShopStates.custom_days)
    await message.answer(
        t(
            "ask_custom_days",
            min=limits["min_days"],
            max=limits["max_days"],
            price=money(int(cfg["custom_day_price"])),
        )
    )


@router.message(ShopStates.custom_days, F.text)
async def custom_days(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    limits = orders.custom_limits(cfg)
    value = parse_int(message.text)
    if value is None or not limits["min_days"] <= value <= limits["max_days"]:
        await message.answer(t("custom_days_invalid", min=limits["min_days"], max=limits["max_days"]))
        return
    await state.update_data(custom_days=value)
    await state.set_state(None)
    await message.answer(t("custom_ready"), reply_markup=menu(t, cfg, staff))
    if await ask_username(message, t, cfg, state):
        return
    await checkout(message, session, customer, t, cfg, state)


@router.message(ShopStates.label, F.text)
async def got_label(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    text = (message.text or "").strip()
    await state.update_data(label=None if text in {"-", "0"} else text[:48])
    await state.set_state(None)
    await message.answer(t("label_saved"), reply_markup=menu(t, cfg, staff))
    if await ask_username(message, t, cfg, state):
        return
    await checkout(message, session, customer, t, cfg, state)


async def _cancel_previous(session: AsyncSession, state: FSMContext, customer: Customer) -> None:
    data = await state.get_data()
    if data.get("order_id"):
        await orders.cancel(session, int(data["order_id"]), customer.id)


async def checkout(
    event: Message | CallbackQuery,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    kind = OrderKind(data.get("kind") or OrderKind.PURCHASE.value)
    plan = (
        await catalog.get_plan(session, customer.shop_id, int(data.get("plan_id") or 0), customer)
        if data.get("plan_id")
        else None
    )
    service = await session.get(Service, int(data["svc_id"])) if data.get("svc_id") else None
    code: DiscountCode | None = None
    if data.get("code_id"):
        code = await session.get(DiscountCode, int(data["code_id"]))
    elif not data.get("no_sticky"):
        code = await pricing.sticky_code(session, customer)
    await _cancel_previous(session, state, customer)
    try:
        draft = await orders.draft(
            session,
            customer,
            kind,
            plan=plan,
            service=service,
            extra_gb=int(data.get("extra_gb") or 0),
            extra_days=int(data.get("extra_days") or 0),
            label=data.get("label"),
            wanted_username=data.get("wanted_username"),
            custom_gb=int(data.get("custom_gb") or 0),
            custom_days=int(data.get("custom_days") or 0),
            package=int(data["package"]) if data.get("package") is not None else None,
            move_to=(
                await catalog.pick_panel_for(session, customer.shop_id, None, int(data["move_loc"]))
                if data.get("move_loc")
                else None
            ),
        )
        try:
            order = await orders.create(session, customer, draft, code=code)
        except pricing.DiscountError:
            if data.get("code_id"):
                raise
            order = await orders.create(session, customer, draft)  # sticky code no longer applies
            code = None
    except orders.OrderError as exc:
        if exc.key == "terms_required":  # the shop asks for its terms before a purchase
            await show(event, t("terms_text"), keyboards.terms(t))
            return
        if exc.key == "reseller_terms_required":
            await show(event, t("reseller_terms_text"), keyboards.terms(t, reseller=True))
            return
        if exc.key == "phone_required":  # and the phone number, when it asks for it only here
            await _ask_phone(event, t)
            return
        await show(event, error_text(t, exc.key, exc.params))
        return
    except pricing.DiscountError as exc:
        await show(event, t(exc.key))
        return
    await state.update_data(order_id=order.id)
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    methods = await payments.available(session, customer.shop_id, customer)
    from paurubot.bot.views import checkout_text

    text = checkout_text(
        t, order, plan=plan, service=service, balance=balance, code=code.code if code else None
    )
    from paurubot.core import bulk

    markup = keyboards.checkout_kb(
        t,
        order_id=order.id,
        can_wallet=balance >= order.amount,
        methods=methods if balance < order.amount else [],
        discount=code is None,
        bulk_plan=plan.id if kind == OrderKind.PURCHASE and plan and bulk.allowed(cfg, customer) >= 2 else 0,
    )
    await session.commit()
    await show(event, text, markup)


@router.callback_query(Shop.filter(F.step == "discount"))
async def ask_discount(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(ShopStates.discount)
    if isinstance(call.message, Message):
        await call.message.answer(t("ask_discount"), reply_markup=keyboards.cancel(t))


@router.message(ShopStates.discount, F.text)
async def got_discount(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    try:
        code = await pricing.lookup_code(session, customer, message.text or "")
    except pricing.DiscountError as exc:
        await message.answer(t(exc.key))
        return
    if code is None:
        await message.answer(t("discount_invalid"))
        return
    data = await state.get_data()
    plan_id = int(data.get("plan_id") or 0) or None
    try:
        await pricing.validate_code(session, code, customer, plan_id=plan_id, amount=10**12)
    except pricing.DiscountError as exc:
        if exc.key != "discount_min_amount":
            await message.answer(t(exc.key))
            return
    await state.update_data(code_id=code.id)
    await state.set_state(None)
    await message.answer(t("discount_applied"), reply_markup=menu(t, cfg, staff))
    await checkout(message, session, customer, t, cfg, state)


@router.callback_query(Shop.filter(F.step == "cancel"))
async def cancel_checkout(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await _cancel_previous(session, state, customer)
    await state.clear()
    await call.answer(t("canceled_short"))
    await show(call, t("checkout_canceled"))


@router.callback_query(Pay.filter(F.method == "wallet"))
async def pay_wallet(
    call: CallbackQuery,
    callback_data: Pay,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    order = await session.get(Order, callback_data.order)
    if order is None or order.customer_id != customer.id or order.status != OrderStatus.PENDING:
        await call.answer(t("order_expired"), show_alert=True)
        return
    try:
        await orders.settle_from_wallet(session, order.id)
    except orders.OrderError as exc:
        await session.rollback()
        await call.answer(error_text(t, exc.key, exc.params), show_alert=True)
        return
    await session.commit()
    await state.clear()
    await call.answer(t("processing"))
    key, values = processing_key(order.params)
    await show(call, t(key, **values))


@router.callback_query(Svc.filter(F.action.in_({"renew", "upgrade"})))
async def renew_or_upgrade(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    service = await session.get(Service, callback_data.id)
    if service is None or service.customer_id != customer.id:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    plans = await catalog.plans(session, customer.shop_id, customer, panel_id=service.panel_id)
    plans = [p for p in plans if p.delivery != "stock"]
    if callback_data.action == "renew":
        plans = [p for p in plans if p.renewable]
    else:
        floor = await orders.upgrade_floor(session, service)
        plans = [p for p in plans if orders.upgradable(p, service, floor)]
    if not plans:
        await call.answer(t("no_plans_for_service"), show_alert=True)
        return
    await call.answer()
    kind = OrderKind.RENEW.value if callback_data.action == "renew" else OrderKind.UPGRADE.value
    await show(
        call,
        t(f"choose_{callback_data.action}_plan"),
        keyboards.plans_kb(
            t,
            await priced(session, plans, customer),
            back_cb=Svc(action="view", id=service.id).pack(),
            svc=service.id,
            kind=kind,
        ),
    )


@router.callback_query(Svc.filter(F.action.in_({"extra_gb", "extra_days"})))
async def extras_menu(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    from paurubot.core import resale

    service = await session.get(Service, callback_data.id)
    gb_price, day_price = (
        await resale.listed_extras(session, service, customer.shop_id, cfg)
        if service is not None
        else (None, None)
    )
    price = gb_price if callback_data.action == "extra_gb" else day_price
    if service is None or service.customer_id != customer.id or price is None:
        await call.answer(t("extras_unavailable"), show_alert=True)
        return
    await call.answer()
    from paurubot.core.orders import extra_choices

    unit = "gb" if callback_data.action == "extra_gb" else "days"
    # The shop's own packages; a reseller's bot sells per unit only.
    packs = [] if await resale.for_shop(session, customer.shop_id) else list(cfg.get("extra_packages") or [])
    mine = [(i, p) for i, p in enumerate(packs) if isinstance(p, dict) and p.get("unit") == unit]
    if callback_data.action == "extra_gb":
        kb = keyboards.extra_kb(t, service.id, "gb", extra_choices(cfg, "gb"), price, mine)
    else:
        kb = keyboards.extra_kb(t, service.id, "days", extra_choices(cfg, "day"), price, mine)
    await show(call, t(f"choose_{callback_data.action}"), kb)


@router.callback_query(Svc.filter(F.action.in_({"pack_gb", "pack_days"})))
async def package_checkout(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    """A ready-made extra package (ClockBot parity): its amount and price come from the shop."""
    await call.answer()
    packs = cfg.get("extra_packages") or []
    if not 0 <= callback_data.value < len(packs):
        await show(call, t("extras_unavailable"))
        return
    pack = packs[callback_data.value]
    is_gb = callback_data.action == "pack_gb"
    await state.set_data(
        {
            "kind": (OrderKind.EXTRA_VOLUME if is_gb else OrderKind.EXTRA_DAYS).value,
            "svc_id": callback_data.id,
            "extra_gb": int(pack.get("amount") or 0) if is_gb else 0,
            "extra_days": 0 if is_gb else int(pack.get("amount") or 0),
            "package": callback_data.value,
            "code_id": 0,
        }
    )
    await checkout(call, session, customer, t, cfg, state)


@router.callback_query(Svc.filter(F.action.in_({"buy_gb", "buy_days"})))
async def extras_checkout(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await call.answer()
    is_gb = callback_data.action == "buy_gb"
    await state.set_data(
        {
            "kind": (OrderKind.EXTRA_VOLUME if is_gb else OrderKind.EXTRA_DAYS).value,
            "svc_id": callback_data.id,
            "extra_gb": callback_data.value if is_gb else 0,
            "extra_days": 0 if is_gb else callback_data.value,
            "code_id": 0,
        }
    )
    await checkout(call, session, customer, t, cfg, state)


@router.callback_query(Svc.filter(F.action == "move"))
async def move_menu(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    """Change the location of a service, keeping its data, end date and link (SVC-04)."""
    service = await session.get(Service, callback_data.id)
    if (
        service is None
        or service.customer_id != customer.id
        or not cfg["move_enabled"]
        or service.is_trial
        or service.status not in orders.MOVABLE
    ):
        await call.answer(t("move_unavailable"), show_alert=True)
        return
    targets = [location for location, _panel in await orders.move_targets(session, service)]
    if not targets:
        await call.answer(t("move_no_targets"), show_alert=True)
        return
    await call.answer()
    price = int(cfg["move_price"] or 0)
    text = t("move_choose") + "\n" + (t("move_price", price=money(price)) if price else t("move_free"))
    await show(call, text, keyboards.move_kb(t, service.id, targets))


@router.callback_query(Svc.filter(F.action == "move_to"))
async def move_checkout(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await call.answer()
    await state.set_data(
        {
            "kind": OrderKind.MOVE.value,
            "svc_id": callback_data.id,
            "move_loc": callback_data.value,
            "code_id": 0,
        }
    )
    await checkout(call, session, customer, t, cfg, state)


# --- several services at once (CAT-07) -------------------------------------------------


@router.callback_query(Shop.filter(F.step == "bulk"))
async def bulk_start(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    from paurubot.core import bulk

    limit = bulk.allowed(cfg, customer)
    plan = await catalog.get_plan(session, customer.shop_id, callback_data.plan, customer)
    if plan is None or limit < 2:
        await call.answer(t("bulk_unavailable"), show_alert=True)
        return
    await call.answer()
    await _cancel_previous(session, state, customer)
    await session.commit()
    label = (await state.get_data()).get("label")
    await state.set_data({"bulk_plan": plan.id, "label": label})
    await state.set_state(ShopStates.bulk_count)
    if isinstance(call.message, Message):
        await call.message.answer(t("ask_bulk_count", max=limit), reply_markup=keyboards.cancel(t))


@router.message(ShopStates.bulk_count, F.text)
async def bulk_count(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    from paurubot.core import bulk

    data = await state.get_data()
    limit = bulk.allowed(cfg, customer)
    count = parse_int(message.text)
    if count is None or not 2 <= count <= limit:
        await message.answer(t("bulk_count", low=2, high=limit))
        return
    plan = await catalog.get_plan(session, customer.shop_id, int(data.get("bulk_plan") or 0), customer)
    if plan is None:
        await state.clear()
        await message.answer(t("plan_unavailable"))
        return
    try:
        one, total = await bulk.quote(session, customer, plan, count)
    except orders.OrderError as exc:
        await message.answer(error_text(t, exc.key, exc.params))
        return
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    await state.set_state(None)
    await state.update_data(bulk_ready={"plan": plan.id, "n": count})
    text = t(
        "bulk_summary",
        n=count,
        plan=esc(plan.title),
        one=money(one),
        total=money(total),
        balance=money(balance),
    )
    if balance < total:
        await message.answer(text + "\n" + t("checkout_need_more", amount=money(total - balance)))
        return
    await message.answer(
        text,
        reply_markup=keyboards.confirm(
            t, Shop(step="bulk_pay", plan=plan.id, n=count).pack(), Shop(step="cancel").pack()
        ),
    )


@router.callback_query(Shop.filter(F.step == "bulk_pay"))
async def bulk_pay(
    call: CallbackQuery,
    callback_data: Shop,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    from paurubot.core import bulk

    data = await state.get_data()
    ready = data.get("bulk_ready") or {}
    if ready != {"plan": callback_data.plan, "n": callback_data.n}:
        await call.answer(t("order_expired"), show_alert=True)  # already paid, or an old button
        return
    await state.clear()
    plan = await catalog.get_plan(session, customer.shop_id, callback_data.plan, customer)
    if plan is None:
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    try:
        _batch, made = await bulk.buy_many(session, customer, plan, callback_data.n, data.get("label"))
    except orders.OrderError as exc:
        await session.rollback()
        await call.answer()
        if exc.key == "terms_required":
            await show(call, t("terms_text"), keyboards.terms(t))
            return
        if exc.key == "reseller_terms_required":
            await show(call, t("reseller_terms_text"), keyboards.terms(t, reseller=True))
            return
        if exc.key == "phone_required":
            await _ask_phone(call, t)
            return
        await show(call, error_text(t, exc.key, exc.params))
        return
    await session.commit()
    await call.answer()
    await show(call, t("bulk_started", n=len(made)))
