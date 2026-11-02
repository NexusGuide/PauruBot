"""F-12: the Mini App saves the phone from Telegram's signed contact, with the bot's own rules.

Before: the Mini App asked Telegram to share the contact and then polled, waiting for the bot to
receive and save the contact message. When that update was late or lost the customer was stuck
at the gate. Telegram also hands the app a signed copy of the contact; the server now checks
that copy like initData and saves it at once.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from paurubot.api.routes import webapp_gate
from paurubot.core import phones
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer
from paurubot.security.tokens import InitDataError, sign_init_data, validate_shared_contact
from tests.fakes.telegram import TOKEN
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env


def signed_contact(user_id: int, phone: str, *, token: str = TOKEN, age: int = 0) -> str:
    contact = {"user_id": user_id, "phone_number": phone, "first_name": "Sara"}
    return sign_init_data({"contact": json.dumps(contact), "auth_date": str(int(time.time()) - age)}, token)


def test_the_signed_contact_is_checked_like_init_data() -> None:
    shared = validate_shared_contact(signed_contact(7, "+989121234567"), TOKEN, max_age=600)
    assert shared.user_id == 7 and shared.phone == "+989121234567"
    for bad in (
        signed_contact(7, "989121234567", token="123:other"),  # another bot's signature
        signed_contact(7, "989121234567", age=3600),  # stale
        signed_contact(7, "989121234567").replace("989121234567", "989120000000"),  # edited
        sign_init_data({"auth_date": str(int(time.time()))}, TOKEN),  # no contact
        sign_init_data(
            {
                "contact": json.dumps({"user_id": "7", "phone_number": "1"}),
                "auth_date": str(int(time.time())),
            },
            TOKEN,
        ),
        sign_init_data({"contact": "{not json", "auth_date": str(int(time.time()))}, TOKEN),
        "",
    ):
        with pytest.raises(InitDataError):
            validate_shared_contact(bad, TOKEN, max_age=600)


def test_the_bot_and_the_app_accept_the_same_numbers() -> None:
    assert phones.allowed("+98 912 123 4567", ["98"]) == "989121234567"
    assert phones.allowed("09121234567", ["+98"]) == "989121234567"
    assert phones.allowed("14155550100", ["98"]) is None
    assert phones.allowed("14155550100", []) == "14155550100"
    assert phones.allowed("12", None) is None


async def _phone(session, shop, uid: int) -> str | None:  # type: ignore[no-untyped-def]
    row = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == uid)
    )
    assert row is not None
    await session.refresh(row)
    return row.phone


async def test_the_app_saves_the_customers_own_phone_at_once(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    await shop_settings.set_many(
        session, shop.id, {"require_phone": True, "terms_required": True, "phone_country_codes": ["98"]}
    )
    shop_settings.invalidate(shop.id)
    await session.commit()
    assert (await client.get("/api/webapp/me", headers=h(9501))).json()["error"] == "phone_required"
    assert (await client.get("/api/webapp/gate", headers=h(9501))).status_code == 200  # the gate stays open

    async def share(body: object):  # type: ignore[no-untyped-def]
        return await client.post("/api/webapp/gate/phone", headers=h(9501), json=body)

    # Someone else's contact, a refused country, a forged or stale copy: nothing is saved.
    other = await share({"response": signed_contact(9599, "989121234567")})
    assert other.status_code == 403 and other.json()["error"] == "phone_not_yours"
    foreign = await share({"response": signed_contact(9501, "+14155550100")})
    assert foreign.status_code == 422 and foreign.json()["error"] == "phone_country_not_allowed"
    for bad in (
        signed_contact(9501, "989121234567", token="123:other"),
        signed_contact(9501, "989121234567", age=3600),
    ):
        response = await share({"response": bad})
        assert response.status_code == 400 and response.json()["error"] == "invalid_contact"
    assert (await share({"response": "x", "phone": "989121234567"})).status_code == 422  # no extra fields
    assert await _phone(session, shop, 9501) is None

    saved = await share({"response": signed_contact(9501, "+98 912 123 4567")})
    assert saved.status_code == 200, saved.text
    assert saved.json()["requirement"] == "terms_required"  # the phone is done; the terms are next
    assert await _phone(session, shop, 9501) == "989121234567"
    assert (await client.get("/api/webapp/gate", headers=h(9501))).json()["requirement"] == "terms_required"


async def test_a_reseller_bots_app_checks_that_bots_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import resale

    reseller = SimpleNamespace(id=42, parent_id=1)

    async def bots(_session):  # type: ignore[no-untyped-def]
        return [(SimpleNamespace(id=41), "41:token"), (reseller, "42:token")]

    monkeypatch.setattr(resale, "bots", bots)
    assert await webapp_gate._bot_token(SimpleNamespace(shop=reseller, session=None)) == "42:token"
    assert (
        await webapp_gate._bot_token(
            SimpleNamespace(shop=SimpleNamespace(id=1, parent_id=None), session=None)
        )
        == TOKEN
    )
