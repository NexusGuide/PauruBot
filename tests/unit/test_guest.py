"""Guest purchase on the web (CAT-08)."""

from __future__ import annotations

import re

import httpx
import pytest
from sqlalchemy import select, update

from paurubot import kv
from paurubot.core import guest
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Job, JobStatus, Order, OrderStatus, Service
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_gateways import CONFIG, fake  # noqa: F401  (gateway mock fixture)
from tests.unit.test_provisioning import drain


@pytest.fixture
async def store(session, fake):  # type: ignore[no-untyped-def]  # noqa: F811
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Web Shop")
    shop.bot_username = "pauru_test_bot"
    await shop_settings.set_many(
        session,
        shop.id,
        {"guest_buy_enabled": True, "aqayepardakht_enabled": True, **CONFIG["aqayepardakht"]},
    )
    panel = await factories.panel(session, shop)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    plan = await factories.plan(session, shop, panel, price=75_000, title="Monthly web")
    await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(), client=("203.0.113.9", 1234)), base_url="https://t"
    ) as client:
        await kv.reset("guest:start:203.0.113.9")
        yield shop, plan, client
    await kv.reset("guest:start:203.0.113.9")
    registry.test_transports.clear()
    registry._cache.clear()


def _app():  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app

    return create_app()


async def test_closed_unless_switched_on(session, store) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client = store
    assert (await client.get(f"/buy/{shop.id}")).status_code == 200
    await shop_settings.set_many(session, shop.id, {"guest_buy_enabled": False})
    await session.commit()
    shop_settings.invalidate(shop.id)
    assert (await client.get(f"/buy/{shop.id}")).status_code == 404
    assert (
        await client.post(f"/buy/{shop.id}", data={"plan_id": plan.id, "method": "aqayepardakht"})
    ).status_code == 404


async def test_web_purchase_end_to_end_and_claim(session, store) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client = store
    page = await client.get(f"/buy/{shop.id}?lang=en")
    assert "Monthly web" in page.text and "75,000" in page.text and 'value="aqayepardakht"' in page.text
    assert "style=" not in page.text  # the CSP allows no inline styles

    started = await client.post(
        f"/buy/{shop.id}?lang=en", data={"plan_id": plan.id, "method": "aqayepardakht"}
    )
    assert started.status_code == 303
    location = started.headers["location"]
    token = re.match(r"/buy/t/([\w-]+)\?go=1$", location).group(1)  # type: ignore[union-attr]
    go = await client.get(location)
    assert 'http-equiv="refresh" content="0;url=https://panel.aqayepardakht.ir/startpay/T-1"' in go.text

    customer = await guest.by_token(session, token)
    assert customer is not None and customer.is_guest and customer.tags == ["guest"]
    assert notify_telegram.is_guest_chat(customer.telegram_id) and not notify_telegram.is_guest_chat(
        -1001234567890
    )
    payment_id = (await guest.status(session, customer))["payment"].id

    back = await client.get(
        "/api/pay/gw/aqayepardakht",
        params={"pid": payment_id, "transid": "T-1"},
        headers={"accept": "text/html"},
    )
    assert back.status_code == 303 and back.headers["location"] == f"/buy/t/{token}"
    building = await client.get(f"/buy/t/{token}?lang=en")
    assert "being set up" in building.text
    await drain(session, "provision")
    ready = await client.get(f"/buy/t/{token}?lang=en")
    assert "Your service is ready" in ready.text and f"start=claim_{token}" in ready.text
    service = await session.scalar(select(Service).where(Service.customer_id == customer.id))
    assert service is not None and service.panel_username.startswith("g")

    real = await factories.customer(session, shop, telegram_id=88001)
    assert await guest.claim(session, token, real) == 1
    await session.commit()
    await session.refresh(service)
    assert service.customer_id == real.id
    order = await session.scalar(select(Order).where(Order.service_id == service.id))
    assert order is not None and order.customer_id == real.id and order.status == OrderStatus.COMPLETED
    assert await wallet_balance(session, shop.id, real.id) == 0
    with pytest.raises(guest.GuestError):
        await guest.claim(session, token, real)
    assert (await client.get(f"/buy/t/{token}")).status_code == 404  # the key is spent


async def test_bad_choices_and_rate_limit(session, store) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client = store
    wrong = await client.post(f"/buy/{shop.id}?lang=en", data={"plan_id": plan.id, "method": "card"})
    assert wrong.status_code == 400 and "That payment method" in wrong.text
    missing = await client.post(
        f"/buy/{shop.id}?lang=en", data={"plan_id": 999999, "method": "aqayepardakht"}
    )
    assert missing.status_code == 400 and "That plan" in missing.text
    for _ in range(guest_limit() - 2):
        await client.post(f"/buy/{shop.id}", data={"plan_id": plan.id, "method": "aqayepardakht"})
    blocked = await client.post(
        f"/buy/{shop.id}?lang=en", data={"plan_id": plan.id, "method": "aqayepardakht"}
    )
    assert blocked.status_code == 400 and "Too many attempts" in blocked.text
    guests = await session.scalars(
        select(Customer).where(Customer.shop_id == shop.id, Customer.guest_token.is_not(None))
    )
    assert len(list(guests)) == guest_limit() - 2  # refused attempts create nothing


def guest_limit() -> int:
    from paurubot.api.routes import guest as routes

    return routes.STARTS_PER_IP


async def test_claim_rules(session, store) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client = store
    other_shop = await factories.shop(session, "Other")
    stranger = await factories.customer(session, other_shop, telegram_id=88002)
    visitor = await guest.new_guest(session, shop.id, "fa")
    with pytest.raises(guest.GuestError):
        await guest.claim(session, str(visitor.guest_token), stranger)  # another shop's guest
    with pytest.raises(guest.GuestError):
        await guest.claim(session, "nope", stranger)
