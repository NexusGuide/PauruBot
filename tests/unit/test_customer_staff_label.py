"""Staff can always tell a customer apart, even one without a Telegram username.

A customer with no @username was listed under their first name alone, which can be a single emoji
(«🪶»): on the sold admin accounts page, and every other staff list, nobody could tell who it was.
Staff screens now show the Telegram id next to the name when there is no username.
"""

from __future__ import annotations

from paurubot.api.routes.admin.accounts import _account_row
from paurubot.db.models import Customer, PanelAccount


def _customer(**fields) -> Customer:  # type: ignore[no-untyped-def]
    return Customer(id=7, shop_id=1, **fields)


def test_a_username_is_enough() -> None:
    assert _customer(telegram_id=5025186201, username="ali_r", first_name="Ali").staff_label == "@ali_r"


def test_without_a_username_the_id_goes_next_to_the_name() -> None:
    assert _customer(telegram_id=5025186201, username=None, first_name="🪶").staff_label == "🪶 · 5025186201"
    assert _customer(telegram_id=5025186201, username=None, first_name="  ").staff_label == "5025186201"
    assert _customer(telegram_id=5025186201, username=None, first_name=None).staff_label == "5025186201"


def test_a_web_guest_has_no_telegram_id_to_show() -> None:
    guest = _customer(telegram_id=-42, username=None, first_name="Sara")
    assert guest.staff_label == "Sara · #7"
    assert _customer(telegram_id=-42, username=None, first_name=None).staff_label == "#7"


def test_the_sold_accounts_list_uses_it() -> None:
    account = PanelAccount(id=1, shop_id=1, customer_id=7, panel_id=1, username="r5025186201_80b9e3")
    row = _account_row(account, _customer(telegram_id=5025186201, username=None, first_name="🪶"))
    assert row["customer"] == "🪶 · 5025186201"
