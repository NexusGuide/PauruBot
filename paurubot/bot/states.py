from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShopStates(StatesGroup):
    label = State()
    discount = State()
    extra_amount = State()
    custom_gb = State()
    custom_days = State()
    bulk_count = State()


class WalletStates(StatesGroup):
    amount = State()
    receipt = State()
    gift = State()
    transfer_to = State()
    transfer_amount = State()
    card_number = State()
    card_photo = State()


class ServiceStates(StatesGroup):
    rename = State()


class TicketStates(StatesGroup):
    subject = State()
    message = State()
    reply = State()


class ResellerStates(StatesGroup):
    application = State()
    search = State()
    sub_telegram_id = State()
    sub_discount = State()


class AdminStates(StatesGroup):
    lookup = State()
    adjust = State()
    broadcast = State()
    reject_reason = State()
    ticket_reply = State()
    message = State()
    plan_price = State()
    bulk_price = State()
    setting_value = State()
    gift_amount = State()
    broadcast_button = State()
    premium_emoji = State()
    start_media = State()
    buy_banner = State()
    service_note = State()
