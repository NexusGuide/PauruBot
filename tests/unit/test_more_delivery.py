"""WireGuard files and username/password logins reach the customer (PNL-08, PNL-11)."""

from __future__ import annotations

import httpx
import pytest
from aiogram.methods import SendDocument, SendMessage, SendPhoto
from sqlalchemy import select, update

from paurubot.bot.views import service_card
from paurubot.core import orders
from paurubot.db.models import Job, JobStatus, OrderKind, PanelKind, Service
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import handlers  # noqa: F401  (registers the notify queue)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.fakes.more_panels import FakeMikrotik, FakeWGDashboard
from tests.fakes.telegram import make_bot
from tests.unit.test_provisioning import drain


@pytest.fixture
async def tg(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    yield fake
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def _buy(session, kind: PanelKind, fake, telegram_id: int, **options):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    shop.bot_username = "pauru_test_bot"
    panel = await factories.panel(session, shop, kind, **options)
    registry.test_transports[panel.id] = httpx.MockTransport(fake.handler)
    plan = await factories.plan(session, shop, panel, price=50_000, days=30, data_gb=20)
    cust = await factories.customer(session, shop, telegram_id=telegram_id)
    await wallet.topup(session, shop.id, cust.id, 100_000, provider="card", payment_id=telegram_id)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await drain(session, "notify")
    service = await session.scalar(select(Service).where(Service.customer_id == cust.id))
    return shop, service


async def test_wireguard_purchase_sends_the_conf_and_its_qr(session, tg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app
    from paurubot.config import get_settings

    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    fake = FakeWGDashboard(key="panel-password")
    shop, service = await _buy(session, PanelKind.WGDASHBOARD, fake, 9101, configuration="wg0")
    assert service is not None and service.data_limit == 20 * 1024**3
    document = tg.last(SendDocument)
    assert document.chat_id == 9101 and document.document.filename.endswith(".conf")
    assert document.document.data.startswith(b"[Interface]")
    assert tg.last(SendPhoto).photo.filename == "wireguard.png"
    card = service_card(Texts("en"), service, None)
    assert "WireGuard" in card and "Configs" in card

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://t"
    ) as client:
        page = await client.get(
            f"/s/{service.sub_token}", headers={"accept": "text/html", "user-agent": "Mozilla/5.0"}
        )
        raw = await client.get(f"/s/{service.sub_token}?download=1", headers={"accept": "text/html"})
    assert "data:image/png;base64," in page.text and "?download=1" in page.text
    assert raw.text.startswith("[Interface]") and "attachment" in raw.headers["content-disposition"]


async def test_account_panels_show_username_and_password(session, tg) -> None:  # type: ignore[no-untyped-def]
    fake = FakeMikrotik()
    shop, service = await _buy(session, PanelKind.MIKROTIK, fake, 9102, profile="monthly")
    assert service is not None and service.panel_data["password"]
    # limits are what was sold: the router only reports usage
    assert service.data_limit == 20 * 1024**3 and service.expire_at is not None
    sent = [c for c in tg.calls if isinstance(c, SendMessage) and c.chat_id == 9102]
    assert any(service.panel_data["password"] in (c.text or "") for c in sent)
    assert not any(isinstance(c, SendPhoto) and c.chat_id == 9102 for c in tg.calls)  # nothing to scan
