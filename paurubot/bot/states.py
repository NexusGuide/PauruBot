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
    rbot_token = State()
    service_note = State()


class ManageStates(StatesGroup):
    rename = State()
    custom_date = State()
    extra_price = State()
    # Faoxima mirror (MGMT-10..12): custom range, panel wizard, quick prices, leaf editors.
    range_start = State()
    range_end = State()
    panel_add_name = State()
    panel_add_url = State()
    panel_add_user = State()
    panel_add_secret = State()
    quick_price = State()
    panel_value = State()
    panel_secret = State()
    panel_hidden = State()
    # Faoxima mirror wave 2 (MGMT-20..26): one generic value form for all desks.
    w2_value = State()
