from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShopStates(StatesGroup):
    label = State()
    discount = State()
    extra_amount = State()


class WalletStates(StatesGroup):
    amount = State()
    receipt = State()


class ServiceStates(StatesGroup):
    rename = State()


class TicketStates(StatesGroup):
    subject = State()
    message = State()
    reply = State()


class ResellerStates(StatesGroup):
    application = State()


class AdminStates(StatesGroup):
    lookup = State()
    adjust = State()
    broadcast = State()
    reject_reason = State()
    ticket_reply = State()
