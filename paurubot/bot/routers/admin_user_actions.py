"""One-tap actions on the staff user card: referrals, card-to-card, identity mark, account
transfer, services on/off, zeroing the wallet, channel membership, reminder messages and the
reseller controls (sales bot, base prices, hidden panels, expiry, removal).

Every change is scoped to the current shop, checked against the staff member's permissions and
written to the audit log. Destructive actions ask for a confirmation first."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot import keyboards
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity, forget_join_check, missing_channels
from paurubot.bot.routers.admin import VERIFIED_TAG, actor, customer_card
from paurubot.bot.routers.admin_navigation import menu
from paurubot.bot.states import AdminStates
from paurubot.bot.utils import StaffOnly, clear_customer_input, show
from paurubot.core import confirm, customers, resale
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import Customer, Panel, ResellerProfile, Service, ServiceStatus
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_account, wallet_balance
from paurubot.ledger.posting import transfer
from paurubot.panels import PanelError
from paurubot.provisioning import lifecycle
from paurubot.worker import queue

router = Router(name="admin_user_actions")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

MAX_PRICE = 100_000_000
MAX_EXPIRY_DAYS = 3650


def is_verified(customer: Customer) -> bool:
    return VERIFIED_TAG in (customer.tags or [])


async def _customer(session: AsyncSession, shop: ShopRow, customer_id: int) -> Customer | None:
    return await customers.get(session, shop.id, customer_id)


async def _profile(session: AsyncSession, shop: ShopRow, customer_id: int) -> ResellerProfile | None:
    return await session.scalar(
        select(ResellerProfile).where(
            ResellerProfile.shop_id == shop.id, ResellerProfile.customer_id == customer_id
        )
    )


async def _card(
    call: CallbackQuery, session: AsyncSession, t: Texts, customer: Customer, staff: StaffIdentity
) -> None:
    await session.refresh(customer)
    text, kb = await customer_card(session, t, customer, staff)
    await show(call, text, kb)


async def _confirm(
    t: Texts, section: str, customer_id: int, telegram_id: int, page: int = 0
) -> InlineKeyboardMarkup:
    nonce = await confirm.issue_nonce(f"tg:{telegram_id}", f"user.{section}", f"{customer_id}:{page}")
    return keyboards.confirm(
        t,
        Admin(section=section, action=f"go_{nonce}", id=customer_id, page=page).pack(),
        Admin(section="user", id=customer_id).pack(),
    )


async def _confirmed(call: CallbackQuery, data: Admin, t: Texts) -> bool:
    raw = data.action.removeprefix("go_")
    nonce = int(raw) if data.action.startswith("go_") and raw.isdigit() else 0
    if not await confirm.consume_nonce(
        f"tg:{call.from_user.id}", f"user.{data.section}", f"{data.id}:{data.page}", nonce
    ):
        await call.answer(t("button_expired"), show_alert=True)
        return False
    return True


def _back(t: Texts, customer_id: int) -> InlineKeyboardMarkup:
    return keyboards.back(t, Admin(section="user", id=customer_id).pack())


async def _ask(call: CallbackQuery, text: str, t: Texts) -> None:
    if isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=keyboards.cancel(t))


# --- referrals --------------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "urefx"), StaffOnly("customers.write"))
async def referral_actions(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """`unref`: take the customer out of its referrer's list. `clear`/`go`: release every
    customer this one referred (they stay customers, they just have no referrer anymore)."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    action = callback_data.action
    if action == "unref":
        if customer.referrer_id is None:
            await call.answer(t("adm_ua_no_referrer"), show_alert=True)
            return
        before = customer.referrer_id
        customer.referrer_id = None
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="customer.referrer_removed",
            entity="customer",
            entity_id=customer.id,
            before={"referrer_id": before},
            after={"referrer_id": None},
        )
        await session.commit()
        await call.answer(t("adm_ua_unref_done"), show_alert=True)
        await _card(call, session, t, customer, staff)
        return
    conds = (Customer.shop_id == shop.id, Customer.referrer_id == customer.id)
    count = int(await session.scalar(select(func.count(Customer.id)).where(*conds)) or 0)
    if count == 0:
        await call.answer(t("adm_ua_no_referrals"), show_alert=True)
        return
    if not action.startswith("go"):
        await call.answer()
        await show(
            call,
            t("adm_ua_clear_refs_confirm", name=esc(customer.display_name), count=count),
            await _confirm(t, "urefx", customer.id, call.from_user.id),
        )
        return
    if not await _confirmed(call, callback_data, t):
        return
    await session.execute(update(Customer).where(*conds).values(referrer_id=None))
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.referrals_cleared",
        entity="customer",
        entity_id=customer.id,
        after={"released": count},
    )
    await session.commit()
    await call.answer(t("adm_ua_clear_refs_done", count=count), show_alert=True)
    await _card(call, session, t, customer, staff)


# --- card to card and identity mark -----------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "ucard"), StaffOnly("payments.review"))
async def card_switch(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """`on`: card to card allowed again (normal review). `off`: card to card blocked."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None or callback_data.action not in {"on", "off"}:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    wanted = "blocked" if callback_data.action == "off" else "normal"
    before = customer.card_trust
    if callback_data.action == "on" and before != "blocked":
        await call.answer(t("adm_ua_card_already_on"), show_alert=True)
        return
    if before == wanted:
        await call.answer(t("adm_ua_card_already_off"), show_alert=True)
        return
    customer.card_trust = wanted
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.card_trust",
        entity="customer",
        entity_id=customer.id,
        before={"card_trust": before},
        after={"card_trust": wanted},
    )
    await session.commit()
    await call.answer(
        t("adm_ua_card_on_done" if wanted == "normal" else "adm_ua_card_off_done"), show_alert=True
    )
    await _card(call, session, t, customer, staff)


@router.callback_query(Admin.filter(F.section == "ukyc"), StaffOnly("customers.write"))
async def identity_mark(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """A staff-set "verified" mark on the customer (a tag; no documents are collected)."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None or callback_data.action not in {"on", "off"}:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    wanted = callback_data.action == "on"
    if is_verified(customer) == wanted:
        await call.answer(t("adm_ua_kyc_unchanged"), show_alert=True)
        return
    tags = [tag for tag in (customer.tags or []) if tag != VERIFIED_TAG]
    if wanted:
        tags.append(VERIFIED_TAG)
    customer.tags = tags
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.verified" if wanted else "customer.unverified",
        entity="customer",
        entity_id=customer.id,
    )
    await session.commit()
    await call.answer(t("adm_ua_kyc_on_done" if wanted else "adm_ua_kyc_off_done"), show_alert=True)
    await _card(call, session, t, customer, staff)


# --- account transfer -------------------------------------------------------------------------


async def _find_target(session: AsyncSession, shop: ShopRow, text: str) -> Customer | None:
    query = text.strip()
    if query.lstrip("-").isdigit():
        return await customers.by_telegram(session, shop.id, int(query))
    if query.startswith("@") and len(query) > 1:
        return await session.scalar(
            select(Customer).where(
                Customer.shop_id == shop.id, func.lower(Customer.username) == query[1:].lower()
            )
        )
    return None


def _move_error(source: Customer, target: Customer | None) -> str | None:
    if target is None or target.is_guest:
        return "adm_ua_move_no_target"
    if target.id == source.id:
        return "adm_ua_move_self"
    if source.reseller_profile is not None:
        return "adm_ua_move_reseller"
    return None


@router.callback_query(
    Admin.filter(F.section == "umove"),
    StaffOnly("customers.write"),
    StaffOnly("services.write"),
    StaffOnly("wallet.adjust"),
)
async def account_move(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    source = await _customer(session, shop, callback_data.id)
    if source is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    if not callback_data.action.startswith("go"):
        if source.reseller_profile is not None:
            await call.answer(t("adm_ua_move_reseller"), show_alert=True)
            return
        await call.answer()
        await state.set_state(AdminStates.account_move)
        await state.update_data(customer_id=source.id)
        await _ask(call, t("adm_ua_move_ask", name=esc(source.display_name)), t)
        return
    target = await _customer(session, shop, callback_data.page)
    error = _move_error(source, target)
    if error or target is None:
        await call.answer(t(error or "adm_ua_move_no_target"), show_alert=True)
        return
    if not await _confirmed(call, callback_data, t):
        return
    services = list(
        await session.scalars(
            select(Service).where(
                Service.shop_id == shop.id,
                Service.customer_id == source.id,
                Service.status != ServiceStatus.DELETED,
            )
        )
    )
    for service in services:
        service.customer_id = target.id
    balance = await wallet_balance(session, shop.id, source.id)
    moved = 0
    if balance > 0:
        src = await wallet_account(session, shop.id, source.id)
        dst = await wallet_account(session, shop.id, target.id)
        await transfer(
            session,
            shop_id=shop.id,
            source_id=src.id,
            target_id=dst.id,
            amount=balance,
            kind="transfer",
            idempotency_key=f"account_move:{source.id}:{target.id}:{callback_data.action}",
            reference_type="customer",
            reference_id=target.id,
            memo=f"account moved to customer {target.id}",
            actor=f"staff:{staff.id or call.from_user.id}",
        )
        moved = balance
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.account_moved",
        entity="customer",
        entity_id=source.id,
        after={"to": target.id, "services": [s.id for s in services], "balance": moved},
    )
    await session.commit()
    await call.answer(t("adm_ua_move_done"), show_alert=True)
    await show(
        call,
        t(
            "adm_ua_move_result",
            source=esc(source.display_name),
            target=esc(target.display_name),
            services=len(services),
            balance=money(moved),
        ),
        keyboards.back(t, Admin(section="user", id=target.id).pack()),
    )


@router.message(
    AdminStates.account_move,
    F.text,
    StaffOnly("customers.write"),
    StaffOnly("services.write"),
    StaffOnly("wallet.adjust"),
)
async def account_move_target(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    source = await _customer(session, shop, int(data.get("customer_id") or 0))
    if source is None:
        await clear_customer_input(state)
        await message.answer(t("adm_customer_not_found"), reply_markup=menu(t, cfg, staff))
        return
    target = await _find_target(session, shop, message.text or "")
    error = _move_error(source, target)
    if error or target is None:
        await message.answer(t(error or "adm_ua_move_no_target"))
        return
    await clear_customer_input(state)
    services = int(
        await session.scalar(
            select(func.count(Service.id)).where(
                Service.shop_id == shop.id,
                Service.customer_id == source.id,
                Service.status != ServiceStatus.DELETED,
            )
        )
        or 0
    )
    balance = await wallet_balance(session, shop.id, source.id)
    await message.answer(t("adm_ua_move_target_ok"), reply_markup=menu(t, cfg, staff))
    await message.answer(
        t(
            "adm_ua_move_confirm",
            source=esc(source.display_name),
            target=esc(target.display_name),
            target_id=target.telegram_id,
            services=services,
            balance=money(max(balance, 0)),
        ),
        reply_markup=await _confirm(
            t, "umove", source.id, message.from_user.id if message.from_user else 0, target.id
        ),
    )


# --- all services on / off --------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "upower"), StaffOnly("services.write"))
async def account_power(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    customer = await _customer(session, shop, callback_data.id)
    if customer is None or (
        callback_data.action not in {"on", "off"} and not callback_data.action.startswith("go_")
    ):
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    enable = callback_data.action == "on"
    if not enable and callback_data.action == "off":
        await call.answer()
        await show(
            call,
            t("adm_ua_power_off_confirm", name=esc(customer.display_name)),
            await _confirm(t, "upower", customer.id, call.from_user.id),
        )
        return
    if not enable and not await _confirmed(call, callback_data, t):
        return
    conds = [
        Service.shop_id == shop.id,
        Service.customer_id == customer.id,
        Service.status != ServiceStatus.DELETED,
    ]
    conds.append(
        Service.status == ServiceStatus.DISABLED if enable else Service.status != ServiceStatus.DISABLED
    )
    services = list(await session.scalars(select(Service).where(*conds).order_by(Service.id)))
    if not services:
        await call.answer(t("adm_ua_power_none"), show_alert=True)
        return
    done: list[int] = []
    failed = 0
    for service in services:
        try:
            async with session.begin_nested():
                await lifecycle.set_enabled(session, service, enable, "staff")
        except PanelError:
            failed += 1
            continue
        done.append(service.id)
        if not enable:
            await queue.enqueue(
                session,
                "notify",
                "service_alert",
                {"service_id": service.id, "alert": "disabled_by_staff", "reason": ""},
                shop_id=shop.id,
            )
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.services_on" if enable else "customer.services_off",
        entity="customer",
        entity_id=customer.id,
        after={"services": done, "failed": failed},
    )
    await session.commit()
    await call.answer(t("adm_ua_power_done", done=len(done), failed=failed), show_alert=True)
    await _card(call, session, t, customer, staff)


# --- zero the wallet --------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "uzero"), StaffOnly("wallet.adjust"))
async def zero_balance(
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
    balance = await wallet_balance(session, shop.id, customer.id)
    if balance == 0:
        await call.answer(t("adm_ua_zero_already"), show_alert=True)
        return
    if not callback_data.action.startswith("go"):
        await call.answer()
        await show(
            call,
            t("adm_ua_zero_confirm", name=esc(customer.display_name), balance=money(balance)),
            await _confirm(t, "uzero", customer.id, call.from_user.id),
        )
        return
    if not await _confirmed(call, callback_data, t):
        return
    await wallet.adjust(
        session,
        shop.id,
        customer.id,
        -balance,
        key=f"zero:{customer.id}:{callback_data.action}",
        actor=f"staff:{staff.id or call.from_user.id}",
        memo="balance zeroed by staff",
    )
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="wallet.zeroed",
        entity="customer",
        entity_id=customer.id,
        before={"balance": balance},
        after={"balance": 0},
    )
    await queue.enqueue(
        session, "notify", "wallet_adjusted", {"customer_id": customer.id, "delta": -balance}, shop_id=shop.id
    )
    await session.commit()
    await call.answer(t("adm_ua_zero_done"), show_alert=True)
    await _card(call, session, t, customer, staff)


# --- channel membership and reminder messages -------------------------------------------------


@router.callback_query(Admin.filter(F.section == "uchan"), StaffOnly("customers.read"))
async def channel_check(
    call: CallbackQuery, callback_data: Admin, session: AsyncSession, shop: ShopRow, t: Texts
) -> None:
    from paurubot.db.models import ForcedChannel

    customer = await _customer(session, shop, callback_data.id)
    if customer is None or call.bot is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    channels = int(
        await session.scalar(select(func.count(ForcedChannel.id)).where(ForcedChannel.shop_id == shop.id))
        or 0
    )
    if not channels:
        await call.answer(t("adm_ua_chan_none"), show_alert=True)
        return
    forget_join_check(customer.telegram_id)
    missing = await missing_channels(call.bot, session, customer)
    await call.answer()
    if missing:
        lines = [t("adm_ua_chan_missing", name=esc(customer.display_name))]
        lines += [f"• {esc(channel.title)}" for channel in missing]
        text = "\n".join(lines)
    else:
        text = t("adm_ua_chan_ok", name=esc(customer.display_name))
    await show(call, text, _back(t, customer.id))


@router.callback_query(Admin.filter(F.section == "ucron"), StaffOnly("customers.write"))
async def reminder_switch(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Expiry and low-volume reminders sent by the scheduled jobs, for this customer only."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    customer.alerts_muted = not customer.alerts_muted
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="customer.alerts_muted",
        entity="customer",
        entity_id=customer.id,
        after={"alerts_muted": customer.alerts_muted},
    )
    await session.commit()
    await call.answer(t("adm_ua_cron_off" if customer.alerts_muted else "adm_ua_cron_on"), show_alert=True)
    await _card(call, session, t, customer, staff)


# --- reseller controls ------------------------------------------------------------------------


@router.callback_query(Admin.filter(F.section == "urbotdel"), StaffOnly("resellers.manage"))
async def reseller_bot_remove(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Stop the reseller's sales bot. Its customers and history stay; only staff can allow it back."""
    profile = await _profile(session, shop, callback_data.id)
    if profile is None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    child = await resale.shop_of(session, profile)
    if child is None or not child.bot_token_enc:
        await call.answer(t("adm_ua_rbot_none"), show_alert=True)
        return
    if not callback_data.action.startswith("go"):
        await call.answer()
        await show(
            call,
            t("adm_ua_rbot_del_confirm"),
            await _confirm(t, "urbotdel", profile.customer_id, call.from_user.id),
        )
        return
    if not await _confirmed(call, callback_data, t):
        return
    await resale.disconnect(session, profile, by_staff=True)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="reseller_bot.stopped_by_staff",
        entity="reseller_profile",
        entity_id=profile.id,
    )
    await session.commit()
    await call.answer(t("adm_ua_rbot_del_done"), show_alert=True)
    customer = await _customer(session, shop, profile.customer_id)
    if customer is not None:
        await _card(call, session, t, customer, staff)


PRICE_FIELDS = {"gb": "gb_price", "hour": "hourly_price"}


@router.callback_query(Admin.filter(F.section == "ubase"), StaffOnly("resellers.manage"))
async def base_price_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    profile = await _profile(session, shop, callback_data.id)
    if profile is None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    field = PRICE_FIELDS.get(callback_data.action)
    if field is None:
        await call.answer(t("button_expired"), show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminStates.reseller_price)
    await state.update_data(customer_id=profile.customer_id, price_field=callback_data.action)
    key = "adm_ua_base_gb_ask" if callback_data.action == "gb" else "adm_ua_base_hour_ask"
    await _ask(call, t(key, current=money(getattr(profile, field))), t)


@router.message(AdminStates.reseller_price, F.text, StaffOnly("resellers.manage"))
async def base_price_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    field = PRICE_FIELDS.get(str(data.get("price_field")))
    profile = await _profile(session, shop, int(data.get("customer_id") or 0))
    if profile is None or field is None:
        await clear_customer_input(state)
        await message.answer(t("adm_not_reseller"), reply_markup=menu(t, cfg, staff))
        return
    value = parse_int(message.text)
    if value is None or not 0 <= value <= MAX_PRICE:
        await message.answer(t("number_expected"))
        return
    await clear_customer_input(state)
    before = getattr(profile, field)
    setattr(profile, field, value)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="reseller.base_price",
        entity="reseller_profile",
        entity_id=profile.id,
        before={field: before},
        after={field: value},
    )
    await session.commit()
    await message.answer(t("adm_saved"), reply_markup=menu(t, cfg, staff))
    customer = await _customer(session, shop, profile.customer_id)
    if customer is not None:
        text, kb = await customer_card(session, t, customer, staff)
        await message.answer(text, reply_markup=kb)


@router.callback_query(Admin.filter(F.section == "uhidden"), StaffOnly("customers.write"))
async def hidden_list(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Only the panels hidden from this customer; tapping one shows it again."""
    customer = await _customer(session, shop, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    hidden = [int(p) for p in (customer.hidden_panel_ids or [])]
    if callback_data.action == "unhide":
        if callback_data.page not in hidden:
            await call.answer(t("button_expired"), show_alert=True)
            return
        before = list(hidden)
        hidden.remove(callback_data.page)
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
        await call.answer(t("adm_ua_hidden_shown"))
    else:
        await call.answer()
    panels = (
        list(
            await session.scalars(
                select(Panel).where(Panel.shop_id == shop.id, Panel.id.in_(hidden)).order_by(Panel.id)
            )
        )
        if hidden
        else []
    )
    kb = InlineKeyboardBuilder()
    for panel in panels:
        kb.button(
            text=f"👁 {panel.name}"[:64],
            callback_data=Admin(section="uhidden", action="unhide", id=customer.id, page=panel.id),
        )
    kb.button(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id))
    kb.adjust(1)
    text = (
        t("adm_ua_hidden_title", name=esc(customer.display_name), count=len(panels))
        if panels
        else t("adm_ua_hidden_none")
    )
    await show(call, text, kb.as_markup())


@router.callback_query(Admin.filter(F.section == "uexp"), StaffOnly("resellers.manage"))
async def expiry_ask(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    profile = await _profile(session, shop, callback_data.id)
    if profile is None:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminStates.reseller_expiry)
    await state.update_data(customer_id=profile.customer_id)
    current = jdate(profile.expires_at) if profile.expires_at else t("unlimited")
    await _ask(call, t("adm_ua_exp_ask", current=current), t)


@router.message(AdminStates.reseller_expiry, F.text, StaffOnly("resellers.manage"))
async def expiry_save(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    profile = await _profile(session, shop, int(data.get("customer_id") or 0))
    if profile is None:
        await clear_customer_input(state)
        await message.answer(t("adm_not_reseller"), reply_markup=menu(t, cfg, staff))
        return
    days = parse_int(message.text)
    if days is None or not 0 <= days <= MAX_EXPIRY_DAYS:
        await message.answer(t("number_expected"))
        return
    await clear_customer_input(state)
    before = profile.expires_at
    profile.expires_at = datetime.now(UTC) + timedelta(days=days) if days else None
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="reseller.expiry",
        entity="reseller_profile",
        entity_id=profile.id,
        before={"expires_at": before.isoformat() if before else None},
        after={"expires_at": profile.expires_at.isoformat() if profile.expires_at else None},
    )
    await session.commit()
    shown = jdate(profile.expires_at) if profile.expires_at else t("unlimited")
    await message.answer(t("adm_ua_exp_done", expires=shown), reply_markup=menu(t, cfg, staff))
    customer = await _customer(session, shop, profile.customer_id)
    if customer is not None:
        await session.refresh(customer)
        text, kb = await customer_card(session, t, customer, staff)
        await message.answer(text, reply_markup=kb)


@router.callback_query(Admin.filter(F.section == "udemote"), StaffOnly("resellers.manage"))
async def reseller_remove(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """End the reseller role. The profile, wallet and history are kept (switched off, not
    deleted); a running sales bot is stopped too."""
    profile = await _profile(session, shop, callback_data.id)
    if profile is None or not profile.is_active:
        await call.answer(t("adm_not_reseller"), show_alert=True)
        return
    if not callback_data.action.startswith("go"):
        await call.answer()
        await show(
            call,
            t("adm_ua_demote_confirm"),
            await _confirm(t, "udemote", profile.customer_id, call.from_user.id),
        )
        return
    if not await _confirmed(call, callback_data, t):
        return
    child = await resale.shop_of(session, profile)
    if child is not None and child.bot_token_enc:
        await resale.disconnect(session, profile, by_staff=True)
    profile.is_active = False
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="reseller.removed",
        entity="reseller_profile",
        entity_id=profile.id,
        before={"is_active": True},
        after={"is_active": False},
    )
    await session.commit()
    await call.answer(t("adm_ua_demote_done"), show_alert=True)
    customer = await _customer(session, shop, profile.customer_id)
    if customer is not None:
        await _card(call, session, t, customer, staff)
