from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShopStates(StatesGroup):
    label = State()
    discount = State()
    extra_amount = State()
    custom_gb = State()
    custom_days = State()
    bulk_count = State()
    username = State()


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


class ResaleBotStates(StatesGroup):
    token = State()
    markup = State()
    support = State()
    card = State()
    plan_price = State()


class ResaleCustomizeStates(StatesGroup):
    text = State()
    logo = State()
    channel = State()


class AdminStates(StatesGroup):
    lookup = State()
    order_lookup = State()
    bulk_service_value = State()
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
    rbot_token = State()
    service_note = State()
    config_value = State()
    card_account = State()
    code_new = State()
    panel_value = State()
    growth_value = State()
    prize_new = State()
    lottery_new = State()
    extra_value = State()
