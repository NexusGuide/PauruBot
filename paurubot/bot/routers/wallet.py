from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Pay, Wallet
from paurubot.bot.routers.start import menu
from paurubot.bot.states import WalletStates
from paurubot.bot.utils import Btn, error_text, show
from paurubot.core.formatting import esc, jdate, money, parse_int
from paurubot.db.models import Customer, Order, OrderStatus, Payment, PaymentStatus
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine as payments
from paurubot.payments.base import PaymentError
from paurubot.payments.chains import NETWORKS
from paurubot.worker import queue

log = logging.getLogger(__name__)
router = Router(name="wallet")
PRESETS = [50_000, 100_000, 200_000, 500_000, 1_000_000]
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


async def wallet_text(session: AsyncSession, customer: Customer, t: Texts) -> str:
    from paurubot.core import growth
    from paurubot.core import settings as shop_settings

    balance = await wallet_balance(session, customer.shop_id, customer.id)
    text = t("wallet_home", balance=money(balance), id=customer.telegram_id)
    cfg = await shop_settings.load(session, customer.shop_id)
    tiers = cfg.get("loyalty_tiers") or []
    if cfg.get("loyalty_enabled") and tiers and not customer.is_reseller:
        tier = growth.tier_for(tiers, customer.total_spent)
        if tier:
            text += "\n" + t("tier_line", title=esc(tier["title"]), percent=tier["percent"])
        upcoming = growth.next_tier(tiers, customer.total_spent)
        if upcoming:
            left = int(upcoming["min_spent"]) - int(customer.total_spent)
            text += "\n" + t("tier_next", title=esc(upcoming["title"]), amount=money(left))
    return text


@router.message(Btn("btn_wallet"))
async def wallet_entry(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        await wallet_text(session, customer, t),
        reply_markup=keyboards.wallet_kb(t, transfer=bool(cfg["wallet_transfer_enabled"])),
    )


@router.callback_query(Wallet.filter(F.action == "home"))
async def wallet_home(
    call: CallbackQuery,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    await state.clear()
    await call.answer()
    await show(
        call,
        await wallet_text(session, customer, t),
        keyboards.wallet_kb(t, transfer=bool(cfg["wallet_transfer_enabled"])),
    )


@router.callback_query(Wallet.filter(F.action == "codes"))
async def my_codes(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    """Private discount codes made for this customer (GRW-10)."""
    from paurubot.core import pricing

    await call.answer()
    codes = await pricing.private_codes(session, customer)
    if not codes:
        await show(call, t("my_codes_empty"), keyboards.back(t, Wallet(action="home").pack()))
        return
    lines = [t("my_codes_title")]
    for code in codes:
        value = (
            f"{code.value}%"
            if code.kind.value == "percent"
            else t("my_codes_toman", amount=money(code.value))
        )
        until = jdate(code.expires_at) if code.expires_at else t("unlimited")
        lines.append(t("my_codes_line", code=esc(code.code), value=value, until=until))
    await show(
        call,
        "\n".join(lines),
        keyboards.copy_kb([(t("btn_copy_code", code=code.code), code.code) for code in codes[:5]]),
    )


# --- moving balance to another customer (FIN-08) ------------------------------------------


@router.callback_query(Wallet.filter(F.action == "transfer"))
async def transfer_start(call: CallbackQuery, t: Texts, cfg: dict[str, Any], state: FSMContext) -> None:
    await call.answer()
    if not cfg["wallet_transfer_enabled"]:
        await show(call, t("transfer_off"))
        return
    await state.set_state(WalletStates.transfer_to)
    if isinstance(call.message, Message):
        await call.message.answer(t("transfer_ask_to"), reply_markup=keyboards.cancel(t))


@router.message(WalletStates.transfer_to, F.text)
async def transfer_to(
    message: Message, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    from paurubot.core import transfers

    try:
        recipient = await transfers.find_recipient(session, customer, message.text or "")
    except transfers.TransferError as exc:
        await message.answer(t(exc.key))
        return
    await state.update_data(transfer_to=recipient.id)
    await state.set_state(WalletStates.transfer_amount)
    await message.answer(t("transfer_ask_amount", name=esc(recipient.display_name)))


@router.message(WalletStates.transfer_amount, F.text)
async def transfer_amount(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.core import transfers

    amount = parse_int(message.text)
    data = await state.get_data()
    recipient = await session.get(Customer, int(data.get("transfer_to") or 0))
    if amount is None or recipient is None:
        await message.answer(t("number_expected"))
        return
    try:
        await transfers.check(session, customer, amount)
    except transfers.TransferError as exc:
        await message.answer(error_text(t, exc.key, exc.params))
        return
    await state.update_data(transfer_amount=amount)
    await state.set_state(None)
    await message.answer(
        t("transfer_confirm", amount=money(amount), name=esc(recipient.display_name)),
        reply_markup=keyboards.confirm(
            t,
            Wallet(action="transfer_go", amount=amount, before=message.message_id).pack(),
            Wallet(action="home").pack(),
        ),
    )


@router.callback_query(Wallet.filter(F.action == "transfer_go"))
async def transfer_go(
    call: CallbackQuery,
    callback_data: Wallet,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    from paurubot.core import transfers

    data = await state.get_data()
    recipient = await session.get(Customer, int(data.get("transfer_to") or 0))
    amount = int(data.get("transfer_amount") or 0)
    if recipient is None or amount <= 0 or amount != callback_data.amount:
        await call.answer(t("button_expired"), show_alert=True)
        return
    try:
        await transfers.send(
            session, customer, recipient, amount, key=f"{customer.id}:{callback_data.before}"
        )
    except transfers.TransferError as exc:
        await session.rollback()
        await call.answer(error_text(t, exc.key, exc.params), show_alert=True)
        return
    await queue.enqueue(
        session,
        "notify",
        "transfer_received",
        {"customer_id": recipient.id, "amount": amount, "name": customer.display_name},
        shop_id=customer.shop_id,
        dedupe_key=f"transfer:{customer.id}:{callback_data.before}",
    )
    await session.commit()
    await state.clear()
    await call.answer(t("transfer_done_short"))
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    await show(
        call,
        t("transfer_done", amount=money(amount), name=esc(recipient.display_name), balance=money(balance)),
    )


@router.callback_query(Wallet.filter(F.action == "history"))
async def history(
    call: CallbackQuery,
    callback_data: Wallet,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    await call.answer()
    rows = await wallet.history(
        session, customer.shop_id, customer.id, limit=10, before_id=callback_data.before or None
    )
    if not rows:
        kb = keyboards.wallet_kb(t, transfer=bool(cfg["wallet_transfer_enabled"]))
        await show(call, t("history_empty"), kb)
        return
    lines = [t("history_header")]
    for entry, journal in rows:
        sign = "+" if entry.amount > 0 else "−"
        kind = journal.kind.split(":")[-1]
        lines.append(
            t(
                "history_line",
                sign=sign,
                amount=money(abs(entry.amount)),
                kind=t(f"tx_{kind}"),
                date=jdate(entry.created_at, with_time=True),
            )
        )
    more = rows[-1][0].id if len(rows) == 10 else None
    await show(call, "\n".join(lines), keyboards.history_kb(t, more))


@router.callback_query(Wallet.filter(F.action == "topup"))
async def topup(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    await call.answer()
    low, high = await payments.topup_bounds(session, customer)
    presets = [p for p in PRESETS if low <= p <= high]
    await show(
        call, t("topup_choose_amount", low=money(low), high=money(high)), keyboards.amounts_kb(t, presets)
    )


@router.callback_query(Wallet.filter(F.action == "custom"))
async def custom_amount(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(WalletStates.amount)
    if isinstance(call.message, Message):
        await call.message.answer(t("topup_ask_amount"), reply_markup=keyboards.cancel(t))


@router.message(WalletStates.amount, F.text)
async def typed_amount(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    amount = parse_int(message.text)
    low, high = await payments.topup_bounds(session, customer)
    if amount is None or not low <= amount <= high:
        await message.answer(t("amount_out_of_range", low=money(low), high=money(high)))
        return
    await state.clear()
    methods = await payments.available(session, customer.shop_id, customer)
    if not methods:
        await message.answer(t("no_payment_methods"), reply_markup=menu(t, cfg, staff))
        return
    await message.answer(t("topup_amount_set", amount=money(amount)), reply_markup=menu(t, cfg, staff))
    await message.answer(
        t("topup_choose_method"), reply_markup=keyboards.topup_methods_kb(t, amount, methods)
    )


@router.callback_query(Wallet.filter(F.action == "amount"))
async def preset_amount(
    call: CallbackQuery, callback_data: Wallet, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    methods = await payments.available(session, customer.shop_id, customer)
    if not methods:
        await show(call, t("no_payment_methods"))
        return
    await show(
        call,
        t("topup_choose_method_for", amount=money(callback_data.amount)),
        keyboards.topup_methods_kb(t, callback_data.amount, methods),
    )


@router.callback_query(Pay.filter(F.method != "wallet"))
async def pay(
    call: CallbackQuery,
    callback_data: Pay,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
    bot: Bot,
) -> None:
    order: Order | None = None
    if callback_data.order:
        order = await session.get(Order, callback_data.order)
        if order is None or order.customer_id != customer.id or order.status != OrderStatus.PENDING:
            await call.answer(t("order_expired"), show_alert=True)
            return
    try:
        if order is not None:
            started = await payments.pay_order(session, customer, order, callback_data.method)
        else:
            started = await payments.start(session, customer, callback_data.method, callback_data.amount)
    except PaymentError as exc:
        await session.rollback()
        if exc.key == "card_verify_required" and isinstance(call.message, Message):
            await call.answer()
            await call.message.answer(
                t("card_verify_required"),
                reply_markup=keyboards.single(t("btn_card_verify"), Wallet(action="cardv").pack()),
            )
            return
        await call.answer(error_text(t, exc.key, exc.params), show_alert=True)
        return
    await session.commit()
    await call.answer()
    payment, checkout = started.payment, started.checkout
    message = call.message if isinstance(call.message, Message) else None
    if message is None:
        return
    if payment.provider == "card":
        accounts = "\n".join(
            t("card_line", number=esc(a["number"]), holder=esc(a["holder"]), bank=esc(a["bank"]))
            for a in checkout.instructions["accounts"]
        )
        await state.set_state(WalletStates.receipt)
        await state.update_data(payment_id=payment.id)
        copies = [
            (t("btn_copy_card", last4=a["number"][-4:]), a["number"])
            for a in checkout.instructions["accounts"][:3]
        ]
        copies.append((t("btn_copy_amount"), str(payment.amount)))
        await message.answer(
            t("card_instructions", amount=money(payment.amount), cards=accounts, payment_id=payment.id),
            reply_markup=keyboards.copy_kb(copies),
        )
        await message.answer(t("receipt_send_hint"), reply_markup=keyboards.cancel(t))
    elif payment.provider == "stars":
        await bot.send_invoice(
            chat_id=customer.telegram_id,
            title=t("stars_invoice_title"),
            description=t("stars_invoice_description", amount=money(payment.amount)),
            payload=str(checkout.instructions["payload"]),
            currency="XTR",
            prices=[LabeledPrice(label=t("stars_invoice_title"), amount=int(checkout.instructions["stars"]))],
        )
    elif payment.provider in NETWORKS:
        info = checkout.instructions
        await message.answer(
            t(
                "crypto_instructions_memo" if info.get("memo") else "crypto_instructions",
                network=esc(info["label"]),
                amount=esc(info["amount"]),
                address=esc(info["address"]),
                memo=esc(info.get("memo") or ""),
                toman=money(payment.amount),
                payment_id=payment.id,
                minutes=int(cfg_minutes(payment)),
            ),
            reply_markup=keyboards.copy_kb(
                [
                    (t("btn_copy_address"), str(info["address"])),
                    (t("btn_copy_amount"), str(info["amount"])),
                    (t("btn_copy_memo"), str(info.get("memo") or "")),
                ]
            ),
        )
    elif checkout.redirect_url:
        await message.answer(
            t("gateway_instructions", amount=money(payments.amount_to_pay(payment)), payment_id=payment.id),
            reply_markup=keyboards.url_button(t("btn_open_gateway"), checkout.redirect_url),
        )


# --- registering a card before paying card to card (PAY-18) ---------------------------------


@router.callback_query(Wallet.filter(F.action == "cardv"))
async def card_verify_start(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    from paurubot.core import cards

    await call.answer()
    if await cards.has_verified(session, customer):
        await show(call, t("card_verified_already"))
        return
    await state.set_state(WalletStates.card_number)
    if isinstance(call.message, Message):
        await call.message.answer(t("card_verify_ask_number"), reply_markup=keyboards.cancel(t))


@router.message(WalletStates.card_number, F.text)
async def card_verify_number(message: Message, t: Texts, state: FSMContext) -> None:
    from paurubot.core import cards

    number = cards.digits(message.text or "")
    if len(number) != 16 or not cards.luhn_ok(number):
        await message.answer(t("card_number_invalid"))
        return
    await state.update_data(card_number=number)
    await state.set_state(WalletStates.card_photo)
    await message.answer(t("card_verify_ask_photo"))


@router.message(WalletStates.card_photo, F.photo)
async def card_verify_photo(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.core import cards

    data = await state.get_data()
    assert message.photo
    try:
        card, other = await cards.register(
            session, customer, str(data.get("card_number") or ""), message.photo[-1].file_id
        )
    except cards.CardError as exc:
        await state.clear()
        await message.answer(t(exc.key), reply_markup=menu(t, cfg, staff))
        return
    await queue.enqueue(
        session,
        "notify",
        "card_verify",
        {"card_id": card.id, "other_customer_id": other.id if other else None},
        shop_id=customer.shop_id,
        dedupe_key=f"card:{card.id}:review",
    )
    await session.commit()
    await state.clear()
    await message.answer(t("card_verify_sent", card=card.masked), reply_markup=menu(t, cfg, staff))


def cfg_minutes(payment: Payment) -> float:
    if payment.expires_at is None:
        return 60
    return max(1, (payment.expires_at - payment.created_at).total_seconds() // 60)


@router.callback_query(Wallet.filter(F.action == "gift"))
async def gift_start(call: CallbackQuery, t: Texts, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(WalletStates.gift)
    if isinstance(call.message, Message):
        await call.message.answer(t("gift_ask_code"), reply_markup=keyboards.cancel(t))


@router.message(WalletStates.gift, F.text)
async def gift_code(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    from paurubot.core import gifts

    try:
        amount = await gifts.redeem(session, customer, message.text or "")
    except gifts.GiftError as exc:
        await session.rollback()
        await message.answer(t(exc.key))
        if exc.key == "gift_too_many_attempts":
            await state.clear()
            await message.answer(t("canceled"), reply_markup=menu(t, cfg, staff))
        return
    await session.commit()
    await state.clear()
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    await message.answer(
        t("gift_redeemed", amount=money(amount), balance=money(balance)), reply_markup=menu(t, cfg, staff)
    )


@router.message(WalletStates.receipt, F.photo | F.document | F.text)
async def receipt(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
    bot: Bot,
) -> None:
    data = await state.get_data()
    payment = await session.get(Payment, int(data.get("payment_id") or 0))
    if payment is None or payment.customer_id != customer.id or payment.status != PaymentStatus.PENDING:
        await state.clear()
        await message.answer(t("payment_not_waiting"), reply_markup=menu(t, cfg, staff))
        return
    file_id, image = None, None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        if (message.document.file_size or 0) > MAX_RECEIPT_BYTES:
            await message.answer(t("receipt_too_large"))
            return
        file_id = message.document.file_id
    text = message.caption or message.text
    if not file_id and not text:
        await message.answer(t("receipt_expected"))
        return
    if file_id:
        try:
            downloaded = await bot.download(file_id)
            image = downloaded.read() if downloaded else None
        except Exception:  # hashing is best effort; the receipt still goes to review
            log.warning("could not download receipt for payment %s", payment.id)
    try:
        duplicate = await payments.attach_receipt(session, payment, file_id=file_id, text=text, image=image)
    except PaymentError as exc:
        await message.answer(t(exc.key))
        return
    await queue.enqueue(
        session,
        "notify",
        "receipt_review",
        {"payment_id": payment.id, "duplicate_of": duplicate.id if duplicate else None},
        shop_id=customer.shop_id,
        dedupe_key=f"payment:{payment.id}:review",
    )
    await state.clear()
    await message.answer(t("receipt_received"), reply_markup=menu(t, cfg, staff))


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    payload = query.invoice_payload or ""
    payment_id = int(payload[4:]) if payload.startswith("pay:") and payload[4:].isdigit() else 0
    payment = await session.get(Payment, payment_id)
    ok = (
        payment is not None
        and payment.provider == "stars"
        and payment.status == PaymentStatus.PENDING
        and payment.customer_id == customer.id
        and payment.provider_amount == query.total_amount
        and query.currency == "XTR"
    )
    await query.answer(ok=ok, error_message=None if ok else t("payment_not_waiting"))


@router.message(F.successful_payment)
async def stars_paid(message: Message, session: AsyncSession, customer: Customer, t: Texts) -> None:
    info = message.successful_payment
    assert info is not None
    payload = info.invoice_payload or ""
    if not payload.startswith("pay:") or not payload[4:].isdigit():
        return
    payment = await session.get(Payment, int(payload[4:]))
    if payment is None or payment.customer_id != customer.id:
        return
    ok = await payments.verify(
        session,
        payment.id,
        {
            "currency": info.currency,
            "total_amount": info.total_amount,
            "charge_id": info.telegram_payment_charge_id,
        },
    )
    await session.commit()
    if ok:
        await payments.fulfill_one(session, payment.id)
        await message.answer(t("payment_received"))
