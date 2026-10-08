"""Callback payload factories. Payloads carry IDs only; handlers re-check ownership on every press."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="n"):
    to: str
    arg: int = 0


class Shop(CallbackData, prefix="b"):
    step: str
    loc: int = 0
    cat: int = 0
    plan: int = 0
    svc: int = 0
    kind: str = ""
    n: int = 0


class Pay(CallbackData, prefix="p"):
    method: str
    order: int = 0
    amount: int = 0


class Svc(CallbackData, prefix="s"):
    action: str
    id: int = 0
    page: int = 0
    value: int = 0


class Wallet(CallbackData, prefix="w"):
    action: str
    amount: int = 0
    before: int = 0


class Review(CallbackData, prefix="rv"):
    action: str
    id: int


class Ticket(CallbackData, prefix="t"):
    action: str
    id: int = 0
    page: int = 0


class Admin(CallbackData, prefix="a"):
    section: str
    action: str = "show"
    id: int = 0
    page: int = 0


class Lang(CallbackData, prefix="l"):
    code: str


class Rs(CallbackData, prefix="rs"):
    """The reseller's own panel in the bot (TEN-13)."""

    action: str
    id: int = 0
    page: int = 0
    value: int = 0


class Acc(CallbackData, prefix="acc"):
    """Panel admin accounts (TEN-14)."""

    action: str
    id: int = 0
    value: int = 0
