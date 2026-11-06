"""Guest purchase on the web (CAT-08)."""

from __future__ import annotations

import html
import re
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paurubot import kv
from paurubot.core import guest
from paurubot.core import settings as shop_settings
from paurubot.db import session as db_session
from paurubot.db.models import Customer, Job, JobStatus, Order, OrderStatus, Service
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.conftest import _migrate
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_gateways import CONFIG, fake  # noqa: F401  (gateway mock fixture)
from tests.unit.test_provisioning import drain


@pytest.fixture
async def session(engine):  # type: ignore[no-untyped-def]
    """Real independent commits in a fresh schema; never nest sessions on one connection.

    The global savepoint fixture cannot model durable guest/order payment sessions:
    closing the read-only caller rolls back savepoints released by its inner writer.
    A unique per-test schema permits real commits without leaking rows to other tests.
    """
    schema = f"test_guest_{uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_async_engine(engine.url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with isolated.begin() as conn:
            await conn.run_sync(_migrate)
        maker = async_sessionmaker(isolated, expire_on_commit=False)
        db_session.set_sessionmaker(maker)
        shop_settings.invalidate()
        async with maker() as actual:
            yield actual
    finally:
        db_session.set_sessionmaker(None)  # type: ignore[arg-type]
        await isolated.dispose()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        shop_settings.invalidate()


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


def form_identity(page):  # type: ignore[no-untyped-def]
    match = re.search(r'name="intent" value="([^"<>]+)"', page.text)
    assert page.status_code == 200 and match is not None, page.text
    return match.group(1)


async def form(client, shop_id, plan_id, method="aqayepardakht"):  # type: ignore[no-untyped-def]
    page = await client.get(f"/buy/{shop_id}?new=1&lang=en")
    return {"plan_id": plan_id, "method": method, "intent": form_identity(page)}


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
        f"/buy/{shop.id}?lang=en",
        data={"plan_id": plan.id, "method": "aqayepardakht", "intent": form_identity(page)},
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
    purchase = await guest.status(session, customer)
    payment_id = purchase["payment"].id

    back = await client.get(
        "/api/pay/gw/aqayepardakht",
        params={"pid": payment_id, "transid": "T-1"},
        headers={"accept": "text/html"},
    )
    assert back.status_code == 303 and back.headers["location"] == "/buy/return"
    assert token not in back.text
    returned = await client.get("/buy/return")  # the browser that started the purchase holds the cookie
    assert returned.status_code == 303 and returned.headers["location"] == f"/buy/t/{token}"
    client.cookies.clear()
    lost = await client.get("/buy/return?lang=en")
    assert lost.status_code != 303 and token not in lost.text  # another browser learns nothing
    building = await client.get(f"/buy/t/{token}?lang=en")
    assert "being set up" in building.text
    # HTTP callbacks committed on other connections; refresh the observer's cached
    # order/payment before using that same session to run the provisioning worker.
    await session.refresh(purchase["order"])
    await session.refresh(purchase["payment"])
    await drain(session, "provision")
    # The worker commits on another connection; refresh the observer identity map.
    await session.refresh(purchase["order"])
    assert purchase["order"].status == OrderStatus.COMPLETED
    await session.refresh(customer)
    ready = await client.get(f"/buy/t/{token}?lang=en")
    assert "Your service is ready" in ready.text and f"start=claim_{token}" in ready.text
    service = await session.scalar(select(Service).where(Service.customer_id == customer.id))
    assert service is not None and service.panel_username.startswith("g")

    real = await factories.customer(session, shop, telegram_id=88001)
    spent = customer.total_spent
    assert await guest.claim(session, token, real) == 1
    await session.commit()
    await session.refresh(customer)
    assert customer.total_spent == 0 and real.total_spent == spent  # counted once
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
    wrong = await client.post(f"/buy/{shop.id}?lang=en", data=await form(client, shop.id, plan.id, "card"))
    assert wrong.status_code == 400 and "That payment method" in wrong.text
    missing = await client.post(f"/buy/{shop.id}?lang=en", data=await form(client, shop.id, 999999))
    assert missing.status_code == 400 and "That plan" in missing.text
    for _ in range(guest_limit() - 2):
        await client.post(f"/buy/{shop.id}", data=await form(client, shop.id, plan.id))
    blocked = await client.post(f"/buy/{shop.id}?lang=en", data=await form(client, shop.id, plan.id))
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


def test_visitors_are_counted_per_line() -> None:
    assert guest.visitor_key("203.0.113.9") == "203.0.113.9"
    assert guest.visitor_key("2001:db8:1:2:aaaa::1") == guest.visitor_key("2001:db8:1:2:ffff::9")
    assert guest.visitor_key("2001:db8:1:2::1") != guest.visitor_key("2001:db8:1:3::1")
    assert guest.visitor_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert guest.visitor_key("not-an-ip") == "not-an-ip"


async def test_unpaid_guests_are_cleaned_up(session, store) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from sqlalchemy import update

    from paurubot.db.base import utcnow
    from paurubot.db.models import Payment, PaymentStatus

    shop, plan, _ = store
    old_unpaid = (await guest.start(session, shop.id, plan.id, "aqayepardakht", "fa")).customer
    old_open = (await guest.start(session, shop.id, plan.id, "aqayepardakht", "fa")).customer
    fresh = (await guest.start(session, shop.id, plan.id, "aqayepardakht", "fa")).customer
    await session.execute(
        update(Payment).where(Payment.customer_id == old_unpaid.id).values(status=PaymentStatus.EXPIRED)
    )
    await session.execute(
        update(Customer)
        .where(Customer.id.in_([old_unpaid.id, old_open.id]))
        .values(created_at=utcnow() - timedelta(days=8))
    )
    await session.commit()
    assert await guest.drop_unpaid(session) == 1
    await session.commit()
    left = set(await session.scalars(select(Customer.id).where(Customer.guest_token.is_not(None))))
    assert old_unpaid.id not in left and {old_open.id, fresh.id} <= left


async def test_form_cookie_required_and_replay_keeps_one_purchase(session, store):  # type: ignore[no-untyped-def]
    from sqlalchemy import func

    from paurubot.db.models import GuestCheckoutIntent, Payment

    shop, plan, client = store
    payload = await form(client, shop.id, plan.id)
    no_token = await client.post(f"/buy/{shop.id}", data={"plan_id": plan.id, "method": "aqayepardakht"})
    assert no_token.status_code == 403
    first = await client.post(f"/buy/{shop.id}", data=payload)
    second = await client.post(f"/buy/{shop.id}", data=payload)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    refreshed = await client.get(f"/buy/{shop.id}")
    assert form_identity(refreshed) == payload["intent"]
    for model in (Customer, Order, Payment, GuestCheckoutIntent):
        assert (
            await session.scalar(select(func.count()).select_from(model).where(model.shop_id == shop.id)) == 1
        )
    client.cookies.clear()
    assert (await client.post(f"/buy/{shop.id}", data=payload)).status_code == 403


async def test_uncertain_guest_page_never_exposes_stale_gateway_link(session, store, monkeypatch):  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from paurubot.payments import engine
    from paurubot.payments.base import PaymentError

    shop, plan, client = store
    create = AsyncMock(side_effect=PaymentError("gateway_error"))
    monkeypatch.setattr(engine.provider("aqayepardakht"), "create", create)
    payload = await form(client, shop.id, plan.id)
    first = await client.post(f"/buy/{shop.id}", data=payload)
    assert first.status_code == 303
    token = re.match(r"/buy/t/([\w-]+)\?go=1$", first.headers["location"]).group(1)
    customer = await guest.by_token(session, token)
    payment = (await guest.status(session, customer))["payment"]
    payment.meta = {**payment.meta, "redirect_url": "https://must-not-show.invalid"}
    await session.commit()
    page = await client.get(first.headers["location"] + "&lang=en")
    assert page.status_code == 200 and "must-not-show" not in page.text
    assert "new=1" not in page.text and "0;url=" not in page.text
    store_page = await client.get(f"/buy/{shop.id}")
    assert form_identity(store_page) == payload["intent"]
    assert "new=1" not in store_page.text  # no invitation to replace an unresolved purchase
    again = await client.post(f"/buy/{shop.id}", data=payload)
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    create.assert_awaited_once()


async def test_changed_order_price_never_resumes_guest_invoice(session, store):  # type: ignore[no-untyped-def]
    shop, plan, client = store
    payload = await form(client, shop.id, plan.id)
    first = await client.post(f"/buy/{shop.id}", data=payload)
    assert first.status_code == 303
    ready = await client.get(first.headers["location"])
    assert 'http-equiv="refresh" content="0;url=' in ready.text
    order = await session.scalar(select(Order).where(Order.shop_id == shop.id))
    assert order is not None
    order.amount += 1
    await session.commit()
    changed = await client.get(first.headers["location"])
    assert changed.status_code == 200 and "0;url=" not in changed.text
    assert 'class="btn primary" href=' not in changed.text


async def test_guest_attempt_visible_before_provider_and_survives_observer_rollback(
    session, store, monkeypatch
):  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from sqlalchemy import func

    from paurubot.db.models import GuestCheckoutIntent, Payment, Shop
    from paurubot.payments import engine, order_checkout
    from paurubot.security import guest_form

    shop, plan, client = store
    sid, pid, original_title = shop.id, plan.id, shop.title
    payload = await form(client, sid, pid)
    maker = db_session.get_sessionmaker()
    provider = engine.provider("aqayepardakht")
    actual_create = provider.create

    async def create(payment, cfg):  # type: ignore[no-untyped-def]
        # A separate connection must see the durable attempt BEFORE remote I/O.
        async with maker() as observer:
            stored = await observer.get(Payment, payment.id)
            intent = await observer.get(GuestCheckoutIntent, guest_form.digest(payload["intent"]))
            assert stored is not None and intent is not None
            assert (intent.customer_id, intent.order_id) == (stored.customer_id, stored.order_id)
            assert stored.meta[order_checkout.ATTEMPT_KEY]["state"] == "uncertain"
        return await actual_create(payment, cfg)

    remote = AsyncMock(side_effect=create)
    monkeypatch.setattr(provider, "create", remote)
    shop.title = "observer-only change must not be committed"
    first = await client.post(f"/buy/{sid}", data=payload)
    assert first.status_code == 303
    await session.rollback()
    async with maker() as observer:
        assert (await observer.get(Shop, sid)).title == original_title
        for model in (Customer, Order, Payment, GuestCheckoutIntent):
            assert (
                await observer.scalar(select(func.count()).select_from(model).where(model.shop_id == sid))
                == 1
            )
    ready = await client.get(first.headers["location"])
    assert 'http-equiv="refresh" content="0;url=' in ready.text
    again = await client.post(f"/buy/{sid}", data=payload)
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    remote.assert_awaited_once()


async def test_a_waiting_buyer_can_change_the_plan(session, store) -> None:  # type: ignore[no-untyped-def]
    """Back on /buy after picking a plan, the buyer sees what they chose and can start over."""
    shop, plan, client = store
    payload = await form(client, shop.id, plan.id)
    assert (await client.post(f"/buy/{shop.id}?lang=en", data=payload)).status_code == 303
    page = await client.get(f"/buy/{shop.id}?lang=en")
    assert "Monthly web" in page.text and "75,000" in page.text  # what was chosen, and its price
    assert f"/buy/{shop.id}?lang=en&amp;new=1" in page.text and "Change plan" in page.text
    fresh = await client.get(f"/buy/{shop.id}?lang=en&new=1")
    assert form_identity(fresh) != payload["intent"] and 'name="plan"' in fresh.text  # a new form


async def test_admin_account_plans_on_the_web(session, store) -> None:  # type: ignore[no-untyped-def]
    """Panel admin account plans are sold on the web when the shop sells them to everyone; the
    page then shows the panel login, and claiming moves the account to the Telegram customer."""
    from paurubot.db.models import AccountPlan, PanelAccount, PanelKind, PlanAudience
    from paurubot.security import secretbox

    shop, _plan, client = store
    pg = await factories.panel(session, shop, PanelKind.PASARGUARD)
    fake_pg = FakeTokenPanel("pasarguard")
    registry.test_transports[pg.id] = transport(fake_pg)
    admin_plan = AccountPlan(
        shop_id=shop.id,
        panel_id=pg.id,
        title="Reseller 500",
        role_id=2,
        data_gb=500,
        max_users=40,
        days=30,
        price=600_000,
    )
    for_resellers = AccountPlan(
        shop_id=shop.id,
        panel_id=pg.id,
        title="Wholesale",
        role_id=2,
        price=1,
        audience=PlanAudience.RESELLERS,
    )
    session.add_all([admin_plan, for_resellers])
    await session.commit()
    page = await client.get(f"/buy/{shop.id}?lang=en&new=1")
    assert "Reseller 500" not in page.text  # admin accounts are off
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    shop_settings.invalidate(shop.id)
    page = await client.get(f"/buy/{shop.id}?lang=en&new=1")
    assert "Reseller 500" in page.text and f'value="a:{admin_plan.id}"' in page.text
    assert "Wholesale" not in page.text  # a plan for resellers only stays off the web

    started = await client.post(
        f"/buy/{shop.id}?lang=en",
        data={"plan": f"a:{admin_plan.id}", "method": "aqayepardakht", "intent": form_identity(page)},
    )
    assert started.status_code == 303
    token = re.match(r"/buy/t/([\w-]+)\?go=1$", started.headers["location"]).group(1)  # type: ignore[union-attr]
    customer = await guest.by_token(session, token)
    purchase = await guest.status(session, customer)
    assert purchase["order"].kind.value == "account_purchase" and purchase["order"].amount == 600_000
    back = await client.get(
        "/api/pay/gw/aqayepardakht",
        params={"pid": purchase["payment"].id, "transid": "T-1"},
        headers={"accept": "text/html"},
    )
    assert back.status_code == 303
    await session.refresh(purchase["order"])
    await session.refresh(purchase["payment"])
    await drain(session, "provision")
    await session.refresh(purchase["order"])
    assert purchase["order"].status == OrderStatus.COMPLETED
    account = await session.scalar(select(PanelAccount).where(PanelAccount.customer_id == customer.id))
    assert account is not None
    # A password with HTML characters is shown escaped, never as raw markup.
    account.password_enc = secretbox.encrypt("panel", 'p&ss<"word')
    await session.commit()
    ready = await client.get(f"/buy/t/{token}?lang=en")
    assert "Your reseller panel is ready" in ready.text
    assert account.username in ready.text and html.escape('p&ss<"word') in ready.text
    assert 'p&ss<"word' not in ready.text
    assert f"start=claim_{token}" in ready.text

    real = await factories.customer(session, shop, telegram_id=88002)
    assert await guest.claim(session, token, real) == 1
    await session.commit()
    await session.refresh(account)
    assert account.customer_id == real.id
    # Switched off on the web: the plan leaves the page.
    await shop_settings.set_many(session, shop.id, {"guest_accounts_enabled": False})
    await session.commit()
    shop_settings.invalidate(shop.id)
    assert "Reseller 500" not in (await client.get(f"/buy/{shop.id}?lang=en&new=1")).text


async def test_no_admin_account_plans_on_a_reseller_bot_shop(session, store, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A reseller's own bot shop never sells admin accounts, so the web never offers them there."""
    from paurubot.core import resale
    from paurubot.db.models import AccountPlan, PanelKind

    shop, _plan, client = store
    pg = await factories.panel(session, shop, PanelKind.PASARGUARD)
    session.add(AccountPlan(shop_id=shop.id, panel_id=pg.id, title="Reseller 500", role_id=2, price=600_000))
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    shop_settings.invalidate(shop.id)
    assert [p.title for p in await guest.account_plans(session, shop.id)] == ["Reseller 500"]

    async def a_reseller_shop(_session, _shop_id):  # type: ignore[no-untyped-def]
        return object()

    monkeypatch.setattr(resale, "for_shop", a_reseller_shop)
    assert await guest.account_plans(session, shop.id) == []
    assert "Reseller 500" not in (await client.get(f"/buy/{shop.id}?lang=en&new=1")).text


async def test_guest_page_style_is_fetched_fresh_after_an_update(store) -> None:  # type: ignore[no-untyped-def]
    """The page style is cached for a day, so its address names the version: an update's new
    look is never held back by a browser's old copy."""
    from paurubot import __version__

    shop, _plan, client = store
    page = await client.get(f"/buy/{shop.id}?lang=en&new=1")
    assert f'href="/s/_/page.css?v={__version__}"' in page.text
