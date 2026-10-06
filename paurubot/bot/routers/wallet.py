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
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    return t("wallet_home", balance=money(balance), id=customer.telegram_id)


@router.message(Btn("btn_wallet"))
async def wallet_entry(
    message: Message, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(await wallet_text(session, customer, t), reply_markup=keyboards.wallet_kb(t))


@router.callback_query(Wallet.filter(F.action == "home"))
async def wallet_home(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await state.clear()
    await call.answer()
    await show(call, await wallet_text(session, customer, t), keyboards.wallet_kb(t))


@router.callback_query(Wallet.filter(F.action == "history"))
async def history(
    call: CallbackQuery, callback_data: Wallet, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    rows = await wallet.history(
        session, customer.shop_id, customer.id, limit=10, before_id=callback_data.before or None
    )
    if not rows:
        await show(call, t("history_empty"), keyboards.wallet_kb(t))
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
    methods = await payments.available(session, customer.shop_id)
    await message.answer(t("topup_amount_set", amount=money(amount)), reply_markup=menu(t, cfg, staff))
    await message.answer(
        t("topup_choose_method"), reply_markup=keyboards.topup_methods_kb(t, amount, methods)
    )


@router.callback_query(Wallet.filter(F.action == "amount"))
async def preset_amount(
    call: CallbackQuery, callback_data: Wallet, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    methods = await payments.available(session, customer.shop_id)
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
        await message.answer(
            t("card_instructions", amount=money(payment.amount), cards=accounts, payment_id=payment.id),
            reply_markup=keyboards.cancel(t),
        )
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
            )
        )
    elif checkout.redirect_url:
        await message.answer(
            t("gateway_instructions", amount=money(payment.amount), payment_id=payment.id),
            reply_markup=keyboards.url_button(t("btn_open_gateway"), checkout.redirect_url),
        )


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
